from __future__ import unicode_literals

from contextlib import contextmanager
import getpass
import glob
import logging
import os
import shelve
import sys
import tempfile
from threading import RLock

from future.utils import PY2, python_2_unicode_compatible
from six import text_type

from ..configuration import Configuration
from .protocol import AutodiscoverProtocol

log = logging.getLogger(__name__)


def shelve_filename():
    # Add the version of the cache format to the filename. If we change the format of the cached data, this version
    # must be bumped. Otherwise, new versions of this package cannot open cache files generated by older versions.
    version = 2
    # 'shelve' may pickle objects using different pickle protocol versions. Append the python major+minor version
    # numbers to the filename. Also append the username, to avoid permission errors.
    major, minor = sys.version_info[:2]
    try:
        user = getpass.getuser()
    except KeyError:
        # getuser() fails on some systems. Provide a sane default. See issue #448
        user = 'exchangelib'
    return 'exchangelib.{version}.cache.{user}.py{major}{minor}'.format(
        version=version, user=user, major=major, minor=minor
    )


AUTODISCOVER_PERSISTENT_STORAGE = os.path.join(tempfile.gettempdir(), shelve_filename())


@contextmanager
def shelve_open_with_failover(filename):
    # We can expect empty or corrupt files. Whatever happens, just delete the cache file and try again.
    # 'shelve' may add a backend-specific suffix to the file, so also delete all files with a suffix.
    # We don't know which file caused the error, so just delete them all.
    try:
        shelve_handle = shelve.open(filename)
    except Exception as e:
        for f in glob.glob(filename + '*'):
            log.warning('Deleting invalid cache file %s (%r)', f, e)
            os.unlink(f)
        shelve_handle = shelve.open(filename)
    yield shelve_handle
    if PY2:
        shelve_handle.close()


@python_2_unicode_compatible
class AutodiscoverCache(object):
    """Stores the translation from (email domain, credentials) -> AutodiscoverProtocol object so we can re-use TCP
    connections to an autodiscover server within the same process. Also persists the email domain -> (autodiscover
    endpoint URL, auth_type) translation to the filesystem so the cache can be shared between multiple processes.

    According to Microsoft, we may forever cache the (email domain -> autodiscover endpoint URL) mapping, or until
    it stops responding. My previous experience with Exchange products in mind, I'm not sure if I should trust that
    advice. But it could save some valuable seconds every time we start a new connection to a known server. In any
    case, the persistent storage must not contain any sensitive information since the cache could be readable by
    unprivileged users. Domain, endpoint and auth_type are OK to cache since this info is make publicly available on
    HTTP and DNS servers via the autodiscover protocol. Just don't persist any credentials info.

    If an autodiscover lookup fails for any reason, the corresponding cache entry must be purged.

    'shelve' is supposedly thread-safe and process-safe, which suits our needs.
    """
    def __init__(self):
        self._protocols = {}  # Mapping from (domain, credentials) to AutodiscoverProtocol
        self._lock = RLock()

    @property
    def _storage_file(self):
        return AUTODISCOVER_PERSISTENT_STORAGE

    def clear(self):
        # Wipe the entire cache
        with shelve_open_with_failover(self._storage_file) as db:
            db.clear()
        self._protocols.clear()

    def __contains__(self, key):
        domain = key[0]
        with shelve_open_with_failover(self._storage_file) as db:
            return str(domain) in db

    def __getitem__(self, key):
        protocol = self._protocols.get(key)
        if protocol:
            return protocol
        domain, credentials = key
        with shelve_open_with_failover(self._storage_file) as db:
            endpoint, auth_type, retry_policy = db[str(domain)]  # It's OK to fail with KeyError here
        protocol = AutodiscoverProtocol(config=Configuration(
            service_endpoint=endpoint, credentials=credentials, auth_type=auth_type, retry_policy=retry_policy
        ))
        self._protocols[key] = protocol
        return protocol

    def __setitem__(self, key, protocol):
        # Populate both local and persistent cache
        domain = key[0]
        with shelve_open_with_failover(self._storage_file) as db:
            # Don't change this payload without bumping the cache file version in shelve_filename()
            db[str(domain)] = (protocol.service_endpoint, protocol.auth_type, protocol.retry_policy)
        self._protocols[key] = protocol

    def __delitem__(self, key):
        # Empty both local and persistent cache. Don't fail on non-existing entries because we could end here
        # multiple times due to race conditions.
        domain = key[0]
        with shelve_open_with_failover(self._storage_file) as db:
            try:
                del db[str(domain)]
            except KeyError:
                pass
        try:
            del self._protocols[key]
        except KeyError:
            pass

    def close(self):
        # Close all open connections
        for (domain, _), protocol in self._protocols.items():
            log.debug('Domain %s: Closing sessions', domain)
            protocol.close()
            del protocol
        self._protocols.clear()

    def __enter__(self):
        self._lock.__enter__()

    def __exit__(self, *args, **kwargs):
        self._lock.__exit__(*args, **kwargs)

    def __del__(self):
        # pylint: disable=bare-except
        try:
            self.close()
        except Exception:  # nosec
            # __del__ should never fail
            pass

    def __str__(self):
        return text_type(self._protocols)


autodiscover_cache = AutodiscoverCache()