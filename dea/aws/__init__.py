from urllib.parse import urlparse
import botocore
import botocore.session
import logging
import time

log = logging.getLogger(__name__)


def ec2_metadata(timeout=0.1):
    from urllib.request import urlopen
    import json

    try:
        with urlopen('http://169.254.169.254/latest/dynamic/instance-identity/document', timeout=timeout) as resp:
            if resp.getcode() == 200:
                return json.loads(resp.read().decode('utf8'))
            else:
                return None
    except (IOError, json.JSONDecodeError):
        return None


def ec2_current_region():
    cfg = ec2_metadata()
    if cfg is None:
        return None
    return cfg.get('region', None)


def botocore_default_region():
    import botocore.session
    return botocore.session.get_session().get_config_variable('region')


def auto_find_region():
    region_name = botocore_default_region()

    if region_name is None:
        region_name = ec2_current_region()

    if region_name is None:
        raise ValueError('Region name is not supplied and default can not be found')

    return region_name


def make_s3_client(region_name=None,
                   max_pool_connections=32,
                   session=None,
                   use_ssl=True):
    if region_name is None:
        region_name = auto_find_region()

    protocol = 'https' if use_ssl else 'http'

    if session is None:
        session = botocore.session.get_session()

    s3 = session.create_client('s3',
                               region_name=region_name,
                               endpoint_url='{}://s3.{}.amazonaws.com'.format(protocol, region_name),
                               config=botocore.client.Config(max_pool_connections=max_pool_connections))
    return s3


def s3_url_parse(url):
    uu = urlparse(url)
    return uu.netloc, uu.path.lstrip('/')


def s3_fmt_range(range):
    """ None -> None
        (in, out) -> "bytes={in}-{out-1}"
    """
    if range is None:
        return None

    _in, _out = range
    return 'bytes={:d}-{:d}'.format(_in, _out-1)


def s3_ls(url, s3=None):
    bucket, prefix = s3_url_parse(url)

    s3 = s3 or make_s3_client()
    paginator = s3.get_paginator('list_objects_v2')

    n_skip = len(prefix)
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for o in page.get('Contents', []):
            yield o['Key'][n_skip:]


def s3_ls_dir(uri, s3=None):
    bucket, prefix = s3_url_parse(uri)
    prefix = prefix.rstrip('/') + '/'

    s3 = s3 or make_s3_client()
    paginator = s3.get_paginator('list_objects_v2')

    for page in paginator.paginate(Bucket=bucket,
                                   Prefix=prefix,
                                   Delimiter='/'):
        sub_dirs = page.get('CommonPrefixes', [])
        files = page.get('Contents', [])

        for p in sub_dirs:
            yield 's3://{bucket}/{path}'.format(bucket=bucket, path=p['Prefix'])

        for o in files:
            yield 's3://{bucket}/{path}'.format(bucket=bucket, path=o['Key'])


def s3_find(url, pred=None, glob=None, s3=None):
    """ List all objects under certain path

        each s3 object is represented by a SimpleNamespace with attributes:
        - url
        - size
        - last_modified
        - etag
    """
    from ._find import norm_predicate, s3_file_info

    if glob is None and isinstance(pred, str):
        pred, glob = None, pred

    pred = norm_predicate(pred, glob)

    if url[-1] != '/':
        url += '/'

    bucket, prefix = s3_url_parse(url)

    s3 = s3 or make_s3_client()
    paginator = s3.get_paginator('list_objects_v2')

    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for o in page.get('Contents', []):
            o = s3_file_info(o, bucket)
            if pred is not None and pred(o):
                yield o


def get_boto_session(region_name=None, cache=None):
    if region_name is None:
        region_name = auto_find_region()

    if cache is not None:
        sessions = getattr(cache, 'sessions', None)
        if sessions is None:
            sessions = {}
            setattr(cache, 'sessions', sessions)

        session = sessions.get(region_name)
    else:
        sessions, session = {}, None

    if session is None:
        session = botocore.session.Session(session_vars=dict(
            region=('region', 'AWS_DEFAULT_REGION', region_name, None)))
        sessions[region_name] = session

    return session


def get_creds_with_retry(session, max_tries=10, sleep=0.1):
    for _ in range(max_tries):
        creds = session.get_credentials()
        if creds is not None:
            return creds
        time.sleep(sleep)

    return None


def s3_fetch(url, s3=None, **kwargs):
    s3 = s3 or make_s3_client()
    bucket, key = s3_url_parse(url)
    oo = s3.get_object(Bucket=bucket, Key=key, **kwargs)
    return oo['Body'].read()


def s3_get_object_request_maker(region_name=None, credentials=None, ssl=True):
    from botocore.session import get_session
    from botocore.auth import S3SigV4Auth
    from botocore.awsrequest import AWSRequest
    from urllib.request import Request

    session = get_session()

    if region_name is None:
        region_name = auto_find_region()

    if credentials is None:
        managed_credentials = session.get_credentials()
        credentials = managed_credentials.get_frozen_credentials()
    else:
        managed_credentials = None

    protocol = 'https' if ssl else 'http'
    auth = S3SigV4Auth(credentials, 's3', region_name)

    def maybe_refresh_credentials():
        nonlocal credentials
        nonlocal auth

        if not managed_credentials:
            return

        creds = managed_credentials.get_frozen_credentials()
        if creds is credentials:
            return

        log.debug("Refreshed credentials (s3_get_object_request_maker)")

        credentials = creds
        auth = S3SigV4Auth(credentials, 's3', region_name)

    def build_request(bucket=None,
                      key=None,
                      url=None,
                      Range=None):
        if key is None and url is None:
            if bucket is None:
                raise ValueError('Have to supply bucket,key or url')
            # assume bucket is url
            url = bucket

        if url is not None:
            bucket, key = s3_url_parse(url)

        if isinstance(Range, (tuple, list)):
            Range = 'bytes={}-{}'.format(Range[0], Range[1]-1)

        maybe_refresh_credentials()

        headers = {}
        if Range is not None:
            headers['Range'] = Range

        req = AWSRequest(method='GET',
                         url='{}://s3.{}.amazonaws.com/{}/{}'.format(protocol, region_name, bucket, key),
                         headers=headers)

        auth.add_auth(req)

        return Request(req.url,
                       headers=dict(**req.headers),
                       method='GET')

    return build_request
