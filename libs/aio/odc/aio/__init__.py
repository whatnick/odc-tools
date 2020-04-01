import aiobotocore
import asyncio
from types import SimpleNamespace

from odc.aws import auto_find_region, s3_url_parse, s3_fmt_range
from odc.aws._find import norm_predicate, s3_file_info
from odc.ppt import EOS_MARKER
from odc.ppt.async_thread import AsyncThread


async def s3_fetch_object(url, s3, range=None):
    """ returns object with

     On success:
        .url = url
        .data = bytes
        .last_modified -- last modified timestamp
        .range = None | (in,out)
        .error = None

    On failure:
        .url = url
        .data = None
        .last_modified = None
        .range = None | (in, out)
        .error = str| botocore.Exception class
    """
    from botocore.exceptions import ClientError, BotoCoreError

    def result(data=None, last_modified=None, error=None):
        return SimpleNamespace(url=url, data=data, error=error, last_modified=last_modified, range=range)

    bucket, key = s3_url_parse(url)
    extra_args = {}

    if range is not None:
        try:
            extra_args['Range'] = s3_fmt_range(range)
        except Exception:
            return result(error='Bad range passed in: ' + str(range))

    try:
        obj = await s3.get_object(Bucket=bucket, Key=key, **extra_args)
        stream = obj.get('Body', None)
        if stream is None:
            return result(error='Missing Body in response')
        async with stream:
            data = await stream.read()
    except (ClientError, BotoCoreError) as e:
        return result(error=e)
    except Exception as e:
        return result(error="Some Error: " + str(e))

    last_modified = obj.get('LastModified', None)
    return result(data=data, last_modified=last_modified)


async def _s3_find_via_cbk(url, cbk, s3, pred=None, glob=None):
    """ List all objects under certain path

        each s3 object is represented by a SimpleNamespace with attributes:
        - url
        - size
        - last_modified
        - etag
    """
    pred = norm_predicate(pred=pred, glob=glob)

    bucket, prefix = s3_url_parse(url)

    if len(prefix) > 0 and not prefix.endswith('/'):
        prefix = prefix + '/'

    pp = s3.get_paginator('list_objects_v2')

    n_total, n = 0, 0

    async for o in pp.paginate(Bucket=bucket, Prefix=prefix):
        for f in o.get('Contents', []):
            n_total += 1
            f = s3_file_info(f, bucket)
            if pred is None or pred(f):
                n += 1
                await cbk(f)

    return n_total, n


async def s3_find(url, s3, pred=None, glob=None):
    """ List all objects under certain path

        each s3 object is represented by a SimpleNamespace with attributes:
        - url
        - size
        - last_modified
        - etag
    """
    _files = []

    async def on_file(f):
        _files.append(f)

    await _s3_find_via_cbk(url, on_file, s3=s3, pred=pred, glob=glob)

    return _files


async def s3_head_object(url, s3):
    """ Run head_object return Result or Error

        (Result, None) -- on success
        (None, error) -- on failure

    """
    from botocore.exceptions import ClientError, BotoCoreError

    def unpack(url, rr):
        return SimpleNamespace(url=url,
                               size=rr.get('ContentLength', 0),
                               etag=rr.get('ETag', ''),
                               last_modified=rr.get('LastModified'),
                               expiration=rr.get('Expiration'))

    bucket, key = s3_url_parse(url)
    try:
        rr = await s3.head_object(Bucket=bucket, Key=key)
    except (ClientError, BotoCoreError) as e:
        return (None, e)

    return (unpack(url, rr), None)


async def s3_dir(url, s3, pred=None, glob=None):
    """ List s3 "directory" without descending into sub directories.

        pred: predicate for file objects file_info -> True|False
        glob: glob pattern for files only

        Returns: (dirs, files)

        where
          dirs -- list of subdirectories in `s3://bucket/path/` format

          files -- list of objects with attributes: url, size, last_modified, etag
    """
    bucket, prefix = s3_url_parse(url)
    pred = norm_predicate(pred=pred, glob=glob)

    if len(prefix) > 0 and not prefix.endswith('/'):
        prefix = prefix + '/'

    pp = s3.get_paginator('list_objects_v2')

    _dirs = []
    _files = []

    async for o in pp.paginate(Bucket=bucket, Prefix=prefix, Delimiter='/'):
        for d in o.get('CommonPrefixes', []):
            d = d.get('Prefix')
            _dirs.append('s3://{}/{}'.format(bucket, d))
        for f in o.get('Contents', []):
            f = s3_file_info(f, bucket)
            if pred is None or pred(f):
                _files.append(f)

    return _dirs, _files


async def s3_dir_dir(url, depth, dst_q, s3, pred=None):
    """Find directories certain depth from the base, push them to the `dst_q`

    ```
    s3://bucket/a
                 |- b1
                      |- c1/...
                      |- c2/...
                      |- some_file.txt
                 |- b2
                      |- c3/...
    ```

    Given a bucket structure above, calling this function with

    - url s3://bucket/a/
    - depth=1 will produce
         - s3://bucket/a/b1/
         - s3://bucket/a/b2/
    - depth=2 will produce
         - s3://bucket/a/b1/c1/
         - s3://bucket/a/b1/c2/
         - s3://bucket/a/b2/c3/

    Any files are ignored.

    If `pred` is supplied it is expected to be a `str -> bool` mapping, on
    input full path of the sub-directory is given (e.g `a/b1/`) starting from
    root, but not including bucket name. Sub-directory is only traversed
    further if predicate returns True.
    """
    if not url.endswith('/'):
        url = url + '/'

    if depth == 0:
        await dst_q.put(url)
        return

    pp = s3.get_paginator('list_objects_v2')

    async def step(bucket, prefix, depth, work_q, dst_q):

        async for o in pp.paginate(Bucket=bucket, Prefix=prefix, Delimiter='/'):
            for d in o.get('CommonPrefixes', []):
                d = d.get('Prefix')
                if pred is not None and not pred(d):
                    continue

                if depth > 1:
                    await work_q.put((d, depth-1))
                else:
                    d = 's3://{}/{}'.format(bucket, d)
                    await dst_q.put(d)

    bucket, prefix = s3_url_parse(url)
    work_q = asyncio.LifoQueue()
    work_q.put_nowait((prefix, depth))

    while work_q.qsize() > 0:
        _dir, depth = work_q.get_nowait()
        await step(bucket, _dir, depth, work_q, dst_q)


async def s3_walker(url, nconcurrent, s3,
                    guide=None,
                    pred=None,
                    glob=None):
    """

    guide(url, depth, base) -> 'dir'|'skip'|'deep'
    """
    def default_guide(url, depth, base):
        return 'dir'

    if guide is None:
        guide = default_guide

    work_q = asyncio.Queue()
    n_active = 0

    async def step(idx):
        nonlocal n_active

        x = await work_q.get()
        if x is EOS_MARKER:
            return EOS_MARKER

        url, depth, action = x
        depth = depth + 1

        n_active += 1

        _files = []
        if action == 'dir':
            _dirs, _files = await s3_dir(url, s3=s3, pred=pred, glob=glob)

            for d in _dirs:
                action = guide(d, depth=depth, base=url)

                if action != 'skip':
                    if action not in ('dir', 'deep'):
                        raise ValueError('Expect skip|dir|deep got: %s' % action)

                    work_q.put_nowait((d, depth, action))

        elif action == 'deep':
            _files = await s3_find(url, s3=s3, pred=pred, glob=glob)
        else:
            raise RuntimeError('Expected action to be one of deep|dir but found %s' % action)

        n_active -= 1

        # Work queue was already empty and we didn't add any more to traverse
        # and no out-standing work is running
        if work_q.empty() and n_active == 0:
            # Tell all workers in the swarm to stop
            for _ in range(nconcurrent):
                work_q.put_nowait(EOS_MARKER)

        return _files

    work_q.put_nowait((url, 0, 'dir'))

    return step


class S3Fetcher(object):
    def __init__(self,
                 nconcurrent=24,
                 region_name=None,
                 addressing_style='path'):
        from aiobotocore.config import AioConfig

        if region_name is None:
            region_name = auto_find_region()

        s3_cfg = AioConfig(max_pool_connections=nconcurrent,
                           s3=dict(addressing_style=addressing_style))

        self._nconcurrent = nconcurrent
        self._async = AsyncThread()
        self._s3 = None
        self._session = None
        self._closed = False

        async def setup(s3_cfg):
            session = aiobotocore.get_session()
            async with session.create_client('s3',
                                             region_name=region_name,
                                             config=s3_cfg) as s3:
                return (session, s3)

        session, s3 = self._async.submit(setup, s3_cfg).result()
        self._session = session
        self._s3 = s3

    def close(self):
        async def _close(s3):
            await s3.close()

        if not self._closed:
            self._async.submit(_close, self._s3).result()
            self._async.terminate()
            self._closed = True

    def __del__(self):
        self.close()

    def list_dir(self, url):
        """ Returns a future object
        """
        async def action(url, s3):
            return await s3_dir(url, s3=s3)
        return self._async.submit(action, url, self._s3)

    def find_all(self, url, pred=None, glob=None):
        """ List all objects under certain path

        Returns a future object that resolves to a list of s3 object metadata

        each s3 object is represented by a SimpleNamespace with attributes:
        - url
        - size
        - last_modified
        - etag
        """
        if glob is None and isinstance(pred, str):
            pred, glob = None, pred

        async def action(url, s3):
            return await s3_find(url, s3=s3, pred=pred, glob=glob)

        return self._async.submit(action, url, self._s3)

    def find(self, url, pred=None, glob=None):
        """ List all objects under certain path

        Returns an iterator of s3 object metadata

        each s3 object is represented by a SimpleNamespace with attributes:
        - url
        - size
        - last_modified
        - etag
        """
        if glob is None and isinstance(pred, str):
            pred, glob = None, pred

        async def find_to_queue(url, s3, q):
            async def on_file(x):
                await q.put(x)

            try:
                await _s3_find_via_cbk(url, on_file, s3=s3, pred=pred, glob=glob)
            except Exception:
                return False
            finally:
                await q.put(EOS_MARKER)
            return True

        q = asyncio.Queue(1000, loop=self._async.loop)
        ff = self._async.submit(find_to_queue, url, self._s3, q)
        clean_exit = False
        raise_error = False

        try:
            yield from self._async.from_queue(q)
            raise_error = not ff.result()
            clean_exit = True
        finally:
            if not clean_exit:
                ff.cancel()
            if raise_error:
                raise IOError(f"Failed to list: {url}")

    def dir_dir(self, url, depth, pred=None):
        async def action(q, s3):
            try:
                await s3_dir_dir(url, depth, q, s3, pred=pred)
            finally:
                await q.put(EOS_MARKER)

        q = asyncio.Queue(1000, loop=self._async.loop)
        ff = self._async.submit(action, q, self._s3)
        clean_exit = False

        try:
            yield from self._async.from_queue(q)
            ff.result()
            clean_exit = True
        finally:
            if not clean_exit:
                ff.cancel()

    def head_object(self, url):
        return self._async.submit(s3_head_object, url, s3=self._s3)

    def fetch(self, url, range=None):
        """ Returns a future object
        """
        return self._async.submit(s3_fetch_object, url, s3=self._s3, range=range)

    def __call__(self, urls):
        """Fetch a bunch of s3 urls concurrently.

        urls -- sequence of  <url | (url, range)> , where range is (in:int,out:int)|None

        On output is a sequence of result objects, note that order is not
        preserved, but one should get one result for every input.

        Successful results object will contain:
          .url = url
          .data = bytes
          .last_modified -- last modified timestamp
          .range = None | (in,out)
          .error = None

        Failed result looks like this:
          .url = url
          .data = None
          .last_modified = None
          .range = None | (in, out)
          .error = str| botocore.Exception class

        """
        from odc.ppt import future_results

        def generate_requests(urls, s3):
            for url in urls:
                if isinstance(url, tuple):
                    url, range = url
                else:
                    range = None

                yield self._async.submit(s3_fetch_object, url, s3=s3, range=range)

        for rr, ee in future_results(generate_requests(urls, self._s3), self._nconcurrent*2):
            if ee is not None:
                assert(not "s3_fetch_object should not raise exceptions, but did")
            else:
                yield rr
