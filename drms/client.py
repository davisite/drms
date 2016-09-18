from __future__ import absolute_import, division, print_function

import os
import re
import time
import warnings
from six import string_types
from six.moves.urllib.request import urlretrieve
from six.moves.urllib.error import HTTPError, URLError
from six.moves.urllib.parse import urljoin
import pandas as pd
import numpy as np

from .json import HttpJsonClient
from .error import DrmsQueryError, DrmsExportError
from .utils import _pd_to_numeric_coerce, _split_arg, _extract_series_name

__all__ = ['SeriesInfo', 'ExportRequest', 'Client']


class SeriesInfo(object):
    """DRMS series details. Use Client.info() to create an instance."""
    def __init__(self, d, name=None):
        self.json = d
        self.name = name
        self.retention = self.json.get('retention')
        self.unitsize = self.json.get('unitsize')
        self.archive = self.json.get('archive')
        self.tapegroup = self.json.get('tapegroup')
        self.note = self.json.get('note')
        self.primekeys = self.json.get('primekeys')
        self.dbindex = self.json.get('dbindex')
        self.keywords = self._parse_keywords(d['keywords'])
        self.links = self._parse_links(d['links'])
        self.segments = self._parse_segments(d['segments'])

    @staticmethod
    def _parse_keywords(d):
        keys = [
            'name', 'type', 'recscope', 'defval', 'units', 'note', 'linkinfo']
        res = []
        for di in d:
            resi = []
            for k in keys:
                resi.append(di.get(k))
            res.append(tuple(resi))
        if not res:
            res = None  # workaround for older pandas versions
        res = pd.DataFrame(res, columns=keys)
        res.index = res.pop('name')
        res['is_time'] = (res.type == 'time')
        res['is_integer'] = (res.type == 'short')
        res['is_integer'] |= (res.type == 'int')
        res['is_integer'] |= (res.type == 'longlong')
        res['is_real'] = (res.type == 'float')
        res['is_real'] |= (res.type == 'double')
        res['is_numeric'] = (res.is_integer | res.is_real)
        return res

    @staticmethod
    def _parse_links(d):
        keys = ['name', 'target', 'kind', 'note']
        res = []
        for di in d:
            resi = []
            for k in keys:
                resi.append(di.get(k))
            res.append(tuple(resi))
        if not res:
            res = None  # workaround for older pandas versions
        res = pd.DataFrame(res, columns=keys)
        res.index = res.pop('name')
        return res

    @staticmethod
    def _parse_segments(d):
        keys = ['name', 'type', 'units', 'protocol', 'dims', 'note']
        res = []
        for di in d:
            resi = []
            for k in keys:
                resi.append(di.get(k))
            res.append(tuple(resi))
        if not res:
            res = None  # workaround for older pandas versions
        res = pd.DataFrame(res, columns=keys)
        res.index = res.pop('name')
        return res

    def __repr__(self):
        if self.name is None:
            return '<SeriesInfo>'
        else:
            return '<SeriesInfo "%s">' % self.name


class ExportRequest(object):
    """
    Class for handling data export requests. Use Client.export() or
    Client.export_from_id() to create an ExportRequest instance.
    """
    _status_code_ok = 0
    _status_code_notfound = 6
    _status_codes_pending = [1, 2, _status_code_notfound]
    _status_codes_ok_or_pending = [_status_code_ok] + _status_codes_pending

    def __init__(self, d, client, verbose=False):
        self._client = client
        self._verbose = verbose
        self._requestid = None
        self._status = None
        self._download_urls_cache = None
        self._update_status(d)

    @classmethod
    def _create_from_id(cls, requestid, client, verbose=False):
        d = client._json.exp_status(requestid)
        return cls(d, client, verbose)

    def __repr__(self):
        idstr = str(None) if self._requestid is None else (
            '"%s"' % self._requestid)
        return '<ExportRequest id=%s, status=%d>' % (idstr, self._status)

    @staticmethod
    def _parse_data(d):
        keys = ['record', 'filename']
        res = None if d is None else [
            (di.get(keys[0]), di.get(keys[1])) for di in d]
        if not res:
            res = None  # workaround for older pandas versions
        res = pd.DataFrame(res, columns=keys)
        return res

    def _update_status(self, d=None):
        if d is None and self._requestid is not None:
            d = self._client._json.exp_status(self._requestid)
        self._d = d
        self._d_time = time.time()
        self._status = int(self._d.get('status', self._status))
        self._requestid = self._d.get('requestid', self._requestid)
        if self._requestid is None:
            # Apparently 'reqid' is used instead of 'requestid' for certain
            # protocols like 'mpg'
            self._requestid = self._d.get('reqid')
        if self._requestid == '':
            # Use None if the requestid is empty (url_quick + as-is)
            self._requestid = None

    def _raise_on_error(self, notfound_ok=True):
        if self._status in self._status_codes_ok_or_pending:
            if self._status != self._status_code_notfound or notfound_ok:
                return  # request has not failed (yet)
        msg = self._d.get('error')
        if msg is None:
            msg = 'DRMS export request failed.'
        msg += ' [status=%d]' % self._status
        raise DrmsExportError(msg)

    def _generate_download_urls(self):
        """Generate download URLs for the current request."""
        res = self.data.copy()
        data_dir = self.dir

        # Clear first record name for movies, as it is not a DRMS record.
        if self.protocol in ['mpg', 'mp4']:
            if res.record[0].startswith('movie'):
                res.record[0] = None

        # tar exports provide only a single TAR file with full path
        if self.tarfile is not None:
            data_dir = None
            res = pd.DataFrame(
                [(None, self.tarfile)], columns=['record', 'filename'])

        # If data_dir is None, the filename column should contain the full
        # path of the file and we need to extract the basename part. If
        # data_dir contains a directory, the filename column should contain
        # only the basename and we need to join it with the directory.
        if data_dir is None:
            res.rename(columns={'filename': 'fpath'}, inplace=True)
            split_fpath = res.fpath.str.split('/')
            res['filename'] = [sfp[-1] for sfp in split_fpath]
        else:
            res['fpath'] = data_dir + '/' + res.filename

        if self.method.startswith('url'):
            baseurl = self._client.server.http_download_baseurl
        elif self.method.startswith('ftp'):
            baseurl = self._client.server.ftp_download_baseurl
        else:
            raise RuntimeError(
                'Download is not supported for export method "%s"' %
                self.method)

        # Generate download URLs.
        urls = []
        for fp in res.fpath:
            while fp.startswith('/'):
                fp = fp[1:]
            urls.append(urljoin(baseurl, fp))
        res['url'] = urls

        # Remove rows with missing files.
        res = res[res.filename != 'NoDataFile']

        del res['fpath']
        return res

    @staticmethod
    def _next_available_filename(fname):
        """Find next available filename, append a number if neccessary."""
        i = 1
        new_fname = fname
        while os.path.exists(new_fname):
            new_fname = '%s.%d' % (fname, i)
            i += 1
        return new_fname

    @property
    def verbose(self):
        return self._verbose

    @verbose.setter
    def verbose(self, value):
        self._verbose = bool(value)

    @property
    def json(self):
        """Dictionary with the full JSON reply of the last status update"""
        return self._d

    @property
    def id(self):
        """Request ID"""
        return self._requestid

    @property
    def status(self):
        """Export request status"""
        return self._status

    @property
    def method(self):
        """Export method"""
        return self._d.get('method')

    @property
    def protocol(self):
        """Export protocol"""
        return self._d.get('protocol')

    @property
    def dir(self):
        """Common directory of the requested files on the server"""
        if self.has_finished(skip_update=True):
            self._raise_on_error()
        else:
            self.wait()
        data_dir = self._d.get('dir')
        return data_dir if data_dir else None

    @property
    def data(self):
        """
        Records and filenames of the export request.

        Returns a DataFrame containing the records and filenames of the
        export request (DataFrame columns: 'record', 'filename').
        """
        if self.has_finished(skip_update=True):
            self._raise_on_error()
        else:
            self.wait()
        return self._parse_data(self._d.get('data'))

    @property
    def tarfile(self):
        """Filename, if a TAR file was requested"""
        if self.has_finished(skip_update=True):
            self._raise_on_error()
        else:
            self.wait()
        data_tarfile = self._d.get('tarfile')
        return data_tarfile if data_tarfile else None

    @property
    def keywords(self):
        """Filename of textfile containing record keywords"""
        if self.has_finished(skip_update=True):
            self._raise_on_error()
        else:
            self.wait()
        data_keywords = self._d.get('keywords')
        return data_keywords if data_keywords else None

    @property
    def request_url(self):
        """URL of the export request"""
        data_dir = self.dir
        http_baseurl = self._client.server.http_download_baseurl
        if data_dir is None or http_baseurl is None:
            return None
        if data_dir.startswith('/'):
            data_dir = data_dir[1:]
        return urljoin(http_baseurl, data_dir)

    @property
    def urls(self):
        """
        URLs for all downloadable files of the export request.

        Returns a DataFrame containing the records, filenames and URLs of the
        export request (DataFrame columns: 'record', 'filename' and 'url').
        """
        if self._download_urls_cache is None:
            self._download_urls_cache = self._generate_download_urls()
        return self._download_urls_cache

    def has_finished(self, skip_update=False):
        """
        Check if the export request has finished.

        Parameters
        ----------
        skip_update : bool
            If set to True, the export status will not be updated from the
            server, even if it was in pending state after the last status
            update.

        Returns
        -------
        True if the export request has finished or False if the request is
        still pending.
        """
        pending = self._status in self._status_codes_pending
        if not pending:
            return True
        if not skip_update:
            self._update_status()
            pending = self._status in self._status_codes_pending
        return not pending

    def has_succeeded(self, skip_update=False):
        """
        Check if the export request has finished successfully.

        Parameters
        ----------
        skip_update : bool
            If set to True, the export status will not be updated from the
            server, even if it was in pending state after the last status
            update.

        Returns
        -------
        True if the export request has finished successfully or False if the
        request failed or is still pending.
        """
        if not self.has_finished(skip_update):
            return False
        return self._status == self._status_code_ok

    def has_failed(self, skip_update=False):
        """
        Check if the export request has finished unsuccessfully.

        Parameters
        ----------
        skip_update : bool
            If set to True, the export status will not be updated from the
            server, even if it was in pending state after the last status
            update.

        Returns
        -------
        True if the export request has finished unsuccessfully or False if the
        request has succeeded or is still pending.
        """
        if not self.has_finished(skip_update):
            return False
        return self._status not in self._status_codes_ok_or_pending

    def wait(self, timeout=None, sleep=5, retries_notfound=5, verbose=None):
        """
        Wait for the server to process the export request. This method
        continously updates the request status until the server signals
        that the export request has succeeded or failed.

        Parameters
        ----------
        timeout : number or None
            Maximum number of seconds until this method times out. If set to
            None (the default), the status will be updated indefinitely until
            the request succeeded or failed.
        sleep : number or None
            Time in seconds between status updates (defaults to 5 seconds).
            If set to None, a server supplied value is used.
        retries_notfound : integer
            Number of retries in case the request was not found on the server.
            Note that it usually takes a short time until a new request is
            registered on the server, so a value too low might cause an
            exception to be raised, even if the request is valid and will
            eventually show up on the server.
        verbose : bool or None
            Set to True if status messages should be printed to stdout. If set
            to None, the verbose flag of the current ExportRequest instance is
            used instead.

        Returns
        -------
        True if the request succeeded or False if a timeout occured. In case
        of an error an exception is raised.
        """
        if timeout is not None:
            t_start = time.time()
            timeout = float(timeout)
        if sleep is not None:
            sleep = float(sleep)
        retries_notfound = int(retries_notfound)
        if verbose is None:
            verbose = self._verbose

        # We are done, if the request has already finished.
        if self.has_finished(skip_update=True):
            self._raise_on_error()
            return True

        while True:
            if verbose:
                idstr = str(None) if self._requestid is None else (
                    '"%s"' % self._requestid)
                print('Export request pending. [id=%s, status=%d]' % (
                    idstr, self._status))

            # Use the user-provided sleep value or the server's wait value.
            # In case neither is available, wait for 5 seconds.
            wait_secs = self._d.get('wait', 5) if sleep is None else sleep

            # Consider the time that passed since the last status update.
            wait_secs -= (time.time() - self._d_time)
            if wait_secs < 0:
                wait_secs = 0

            if timeout is not None:
                # Return, if we would time out while sleeping.
                if t_start + timeout + wait_secs - time.time() < 0:
                    return False

            if verbose:
                print('Waiting for %d seconds...' % round(wait_secs))
            time.sleep(wait_secs)

            if self.has_finished():
                self._raise_on_error()
                return True
            elif self._status == self._status_code_notfound:
                # Raise exception, if no retries are left.
                if retries_notfound <= 0:
                    self._raise_on_error(notfound_ok=False)
                if verbose:
                    print('Request not found on server, %d retries left.' %
                          retries_notfound)
                retries_notfound -= 1

    def download(self, directory, index=None, fname_from_rec=None,
                 verbose=None):
        """
        Download data files.

        By default, the server-side filenames are used as local filenames,
        except for export method 'url_quick', where the local filenames are
        generated from record names (see parameter fname_from_rec). In case a
        file with the same name already exists in the download directory, an
        ascending number is appended to the filename.

        Note: Downloading data segments that are directories, e.g. data
        segments from series like "hmi.rdVflows_fd15_frame", is currently
        not supported. In order to download data from series like this,
        you need to use the export methods 'url-tar' or 'ftp-tar' when
        submitting the data export request.

        Parameters
        ----------
        directory : string
            Download directory (must already exist).
        index : integer, list of integers or None
            Index (or indices) of the file(s) to be downloaded. If set to
            None (the default), all files of the export request are
            downloaded. Note that this parameter is ignored for export methods
            'url-tar' and 'ftp-tar', where only a single tar file is available
            for download.
        fname_from_rec : bool or None
            If True, local filenames are generated from record names. If set to
            False, the original filenames are used. If set to None (default),
            local filenames are generated only for export method 'url_quick'.
            Exceptions: For exports with methods 'url-tar' and 'ftp-tar', no
            filename will be generated. This also applies to movie files from
            exports with protocols 'mpg' or 'mp4', where the original filename
            is used locally.
        verbose : bool or None
            Set to True if status messages should be printed to stdout. If set
            to None, the verbose flag of the current ExportRequest instance is
            used instead.

        Returns
        -------
        result : pandas.DataFrame
            DataFrame containing the record string, download URL and local
            location of each downloaded file (DataFrame columns: 'record',
            'url' and 'download').
        """
        out_dir = os.path.abspath(directory)
        if not os.path.isdir(out_dir):
            raise IOError('Download directory "%s" does not exist' % out_dir)

        if np.isscalar(index):
            index = [int(index)]
        elif index is not None:
            index = list(index)

        if verbose is None:
            verbose = self.verbose

        # Wait until the export request has finished.
        self.wait(verbose=verbose)

        if fname_from_rec is None:
            # For 'url_quick', generate local filenames from record strings.
            if self.method == 'url_quick':
                fname_from_rec = True

        # self.urls contains the same records as self.data, except for the tar
        # methods, where self.urls only contains one entry, the TAR file.
        data = self.urls
        if index is not None and self.tarfile is None:
            data = data.iloc[index].copy()
        ndata = len(data)

        downloads = []
        for i in range(ndata):
            di = data.iloc[i]
            if fname_from_rec:
                filename = self._client._filename_from_export_record(
                    di.record, old_fname=di.filename)
                if filename is None:
                    filename = di.filename
            else:
                filename = di.filename

            fpath = os.path.join(out_dir, filename)
            fpath_new = self._next_available_filename(fpath)
            fpath_tmp = self._next_available_filename(fpath_new + '.part')
            if verbose:
                print('Downloading file %d of %d...' % (i + 1, ndata))
                print('    record: %s' % di.record)
                print('  filename: %s' % di.filename)
            try:
                urlretrieve(di.url, fpath_tmp)
            except (HTTPError, URLError):
                fpath_new = None
                if verbose:
                    print('  -> Error: Could not download file')
            else:
                fpath_new = self._next_available_filename(fpath)
                os.rename(fpath_tmp, fpath_new)
                if verbose:
                    print('  -> "%s"' % os.path.relpath(fpath_new))
            downloads.append(fpath_new)

        res = data[['record', 'url']].copy()
        res['download'] = downloads
        return res


class Client(object):
    def __init__(self, server='jsoc', email=None, debug=False):
        """
        Client for remote DRMS server access.

        Parameters
        ----------
        server : string or ServerConfig
            Registered server ID or ServerConfig instance.
            Defaults to JSOC.
        email : string or None
            Default email address used data export requests.
        debug : boolean
            Enable or disable debug mode (default is disabled).
        """
        self._json = HttpJsonClient(server=server, debug=debug)
        self._info_cache = {}
        self.email = email  # use property, for email validation

    def __repr__(self):
        return '<Client "%s">' % self.server.name

    def _convert_numeric_keywords(self, ds, kdf, skip_conversion=None):
        si = self.info(ds)
        int_keys = list(si.keywords[si.keywords.is_integer].index)
        num_keys = list(si.keywords[si.keywords.is_numeric].index)
        num_keys += ['*recnum*', '*sunum*', '*size*']
        if skip_conversion is None:
            skip_conversion = []
        elif isinstance(skip_conversion, string_types):
            skip_conversion = [skip_conversion]
        for k in kdf:
            if k in skip_conversion:
                continue
            # pandas apparently does not support hexadecimal strings, so
            # we need a special treatment for integer strings that start
            # with '0x', like QUALITY. The following to_numeric call is
            # still neccessary as the results are still Python objects.
            if k in int_keys and kdf[k].dtype is np.dtype(object):
                idx = kdf[k].str.startswith('0x')
                if idx.any():
                    kdf.loc[idx, k] = kdf.loc[idx, k].map(
                        lambda x: int(x, base=16))
            if k in num_keys:
                kdf[k] = _pd_to_numeric_coerce(kdf[k])

    @staticmethod
    def _raise_query_error(d, status=None):
        """Raises a DrmsQueryError, using the json error message from d"""
        if status is None:
            status = d.get('status')
        msg = d.get('error')
        if msg is None:
            msg = 'DRMS Query failed.'
        msg += ' [status=%s]' % status
        raise DrmsQueryError(msg)

    def _generate_filenamefmt(self, sname):
        """Generate filename format string for export requests."""
        try:
            si = self.info(sname)
        except:
            # Cannot generate filename format for unknown series.
            return None

        pkfmt_list = []
        for k in si.primekeys:
            if si.keywords.loc[k].is_time:
                pkfmt_list.append('{%s:A}' % k)
            else:
                pkfmt_list.append('{%s}' % k)

        if pkfmt_list:
            return '%s.%s.{segment}' % (si.name, '.'.join(pkfmt_list))
        else:
            return si.name + '.{recnum:%lld}.{segment}'

    # Some regular expressions used to parse export request queries.
    _re_export_recset = re.compile(
        r'^\s*([\w\.]+)\s*(\[.*\])?\s*(?:\{([\w\s\.,]*)\})?\s*$')
    _re_export_recset_pkeys = re.compile(r'\[([^\[^\]]*)\]')
    _re_export_recset_slist = re.compile(r'[\s,]+')

    @staticmethod
    def _parse_export_recset(rs):
        """Parse export request record set."""
        if rs is None:
            return None, None, None
        m = Client._re_export_recset.match(rs)
        if not m:
            return None, None, None
        sname, pkeys, segs = m.groups()
        if pkeys is not None:
            pkeys = Client._re_export_recset_pkeys.findall(pkeys)
        if segs is not None:
            segs = Client._re_export_recset_slist.split(segs)
        return sname, pkeys, segs

    def _filename_from_export_record(self, rs, old_fname=None):
        """Generate a filename from an export request record."""
        sname, pkeys, segs = self._parse_export_recset(rs)
        if sname is None:
            return None

        # We need to identify time primekeys and change the time strings to
        # make them suitable for filenames.
        try:
            si = self.info(sname)
        except:
            # Cannot generate filename for unknown series.
            return None

        if pkeys is not None:
            n = len(pkeys)
            if n != len(si.primekeys):
                # Number of parsed pkeys differs from series definition.
                return None
            for i in range(n):
                # Cleanup time strings.
                if si.keywords.loc[si.primekeys[i]].is_time:
                    v = pkeys[i]
                    v = v.replace('.', '').replace(':', '').replace('-', '')
                    pkeys[i] = v

        # Generate filename.
        fname = si.name
        if pkeys is not None:
            pkeys = [k for k in pkeys if k.strip()]
            pkeys_str = '.'.join(pkeys)
            if pkeys_str:
                fname += '.' + pkeys_str
        if segs is not None:
            segs = [s for s in segs if s.strip()]
            segs_str = '.'.join(segs)
            if segs_str:
                fname += '.' + segs_str

        if old_fname is not None:
            # Try to use the file extension of the original filename.
            known_fname_extensions = [
                '.fits', '.txt', '.jpg', '.mpg', '.mp4', '.tar']
            for ext in known_fname_extensions:
                if old_fname.endswith(ext):
                    return fname + ext
        return fname

    @property
    def server(self):
        return self._json.server

    @property
    def debug(self):
        return self._json.debug

    @debug.setter
    def debug(self, value):
        self._json.debug = value

    @property
    def email(self):
        """Default email address used for data export requests."""
        return self._email

    @email.setter
    def email(self, value):
        if value is not None and not self.check_email(value):
            raise ValueError('Email address is invalid or not registered')
        self._email = value

    def series(self, ds_filter=None, full=False):
        """
        List available data series.

        Parameters
        ----------
        ds_filter : string or None
            Regular expression to select a subset of the available series.
            If set to None, a list of all available series is returned.
        full : boolean
            If True, return a DataFrame containing additional series
            information, like description and primekeys. If False (default),
            the result is a list containing only the series names.

        Returns
        -------
        result : list or pandas.DataFrame
            List of series names or DataFrame containing name, primekeys
            and a description of the selected series (see full parameter).
        """
        if self.server.url_show_series_wrapper is None:
            # No wrapper CGI available, use the regular version.
            d = self._json.show_series(ds_filter)
            status = d.get('status')
            if status != 0:
                self._raise_query_error(d)
            if full:
                keys = ('name', 'primekeys', 'note')
                if not d['names']:
                    return pd.DataFrame(columns=keys)
                recs = [(it['name'], _split_arg(it['primekeys']), it['note'])
                        for it in d['names']]
                return pd.DataFrame(recs, columns=keys)
            else:
                if not d['names']:
                    return []
                return [it['name'] for it in d['names']]
        else:
            # Use show_series_wrapper instead of the regular version.
            d = self._json.show_series_wrapper(ds_filter, info=full)
            if full:
                keys = ('name', 'note')
                if not d['seriesList']:
                    return pd.DataFrame(columns=keys)
                recs = []
                for it in d['seriesList']:
                    name, info = tuple(it.items())[0]
                    note = info.get('description', '')
                    recs.append((name, note))
                return pd.DataFrame(recs, columns=keys)
            else:
                return d['seriesList']

    def info(self, ds):
        """
        Get information about the content of a data series.

        Parameters
        ----------
        ds : string
            Name of the data series.

        Returns
        -------
        result : SeriesInfo
            SeriesInfo instance containing information about the data series.
        """
        name = _extract_series_name(ds)
        if name is not None:
            name = name.lower()
        if name in self._info_cache:
            return self._info_cache[name]
        d = self._json.series_struct(name)
        status = d.get('status')
        if status != 0:
            self._raise_query_error(d)
        si = SeriesInfo(d, name=name)
        if name is not None:
            self._info_cache[name] = si
        return si

    def keys(self, ds):
        """
        Get a list of keywords that are available for a series. Use the info()
        method for more details.

        Parameters
        ----------
        ds : string
            Name of the data series.

        Returns
        -------
        result : list
            List of keywords available for the selected series.
        """
        si = self.info(ds)
        return list(si.keywords.index)

    def pkeys(self, ds):
        """
        Get a list of primekeys that are available for a series. Use the info()
        method for more details.

        Parameters
        ----------
        ds : string
            Name of the data series.

        Returns
        -------
        result : list
            List of primekeys available for the selected series.
        """
        si = self.info(ds)
        return list(si.primekeys)

    def get(self, ds, key=None, seg=None, link=None, convert_numeric=True,
            skip_conversion=None):
        """
        This method is deprecated. Use Client.query() instead.
        """
        warnings.warn(
            'Client.get() is deprecated, use Client.query() instead',
            DeprecationWarning)
        return self.query(
            ds, key=key, seg=seg, link=link, convert_numeric=convert_numeric,
            skip_conversion=skip_conversion)

    def query(self, ds, key=None, seg=None, link=None, convert_numeric=True,
              skip_conversion=None, pkeys=False, rec_index=False, n=None):
        """
        Query keywords, segments and/or links of a record set. At least one
        of the arguments key, seg, link or pkeys needs to be specified.

        Parameters
        ----------
        ds : string
            Record set query.
        key : string, list of strings or None
            List of requested keywords, optional. If set to None (default),
            no keyword results will be returned, except when pkeys is True.
        seg : string, list of strings or None
            List of requested segments, optional. If set to None (default),
            no segment results will be returned.
        link : string, list of strings or None
            List of requested Links, optional. If set to None (default), no
            link results will be returned.
        convert_numeric : boolean
            Convert keywords with numeric types from string to numbers. This
            may result in NaNs for invalid/missing values. Default is True.
        skip_conversion : list of strings or None
            List of keywords names to be skipped when performing a numeric
            conversion. Default is None.
        pkeys : boolean
            If True, all primekeys of the series are added to argument key.
        rec_index : boolean
            If True, record names are used as index for the resulting
            DataFrames.
        n : integer or None
            Limits the number of records returned by the query. For positive
            values, the first n records of the record set are returned, for
            negative values the last |n| records. If set to None (default),
            no limit is applied.

        Returns
        -------
        res_key : pandas.DataFrame (if key is not None or pkeys is True)
            Queried keywords
        res_seg : pandas.DataFrame (if seg is not None)
            Queried segments
        res_link : pandas.DataFrame (if link is not None)
            Queried links
        """
        if pkeys:
            pk = self.pkeys(ds)
            key = _split_arg(key) if key is not None else []
            key = [k for k in key if k not in pk]
            key = pk + key

        lres = self._json.rs_list(
            ds, key, seg, link, recinfo=rec_index, n=n)
        status = lres.get('status')
        if status != 0:
            self._raise_query_error(lres)

        res = []
        if key is not None:
            if 'keywords' in lres:
                names = [it['name'] for it in lres['keywords']]
                values = [it['values'] for it in lres['keywords']]
                res_key = pd.DataFrame.from_items(zip(names, values))
            else:
                res_key = pd.DataFrame()
            if convert_numeric:
                self._convert_numeric_keywords(ds, res_key, skip_conversion)
            res.append(res_key)

        if seg is not None:
            if 'segments' in lres:
                names = [it['name'] for it in lres['segments']]
                values = [it['values'] for it in lres['segments']]
                res_seg = pd.DataFrame.from_items(zip(names, values))
            else:
                res_seg = pd.DataFrame()
            res.append(res_seg)

        if link is not None:
            if 'links' in lres:
                names = [it['name'] for it in lres['links']]
                values = [it['values'] for it in lres['links']]
                res_link = pd.DataFrame.from_items(zip(names, values))
            else:
                res_link = pd.DataFrame()
            res.append(res_link)

        if rec_index:
            index = [it['name'] for it in lres['recinfo']]
            for r in res:
                r.index = index

        if len(res) == 0:
            return None
        elif len(res) == 1:
            return res[0]
        else:
            return tuple(res)

    def check_email(self, email):
        """
        Check if the email address is registered for data export. You can
        register your email for data exports from JSOC at

            http://jsoc.stanford.edu/ajax/register_email.html

        Parameters
        ----------
        email : string
            Email address to be checked.

        Returns
        -------
        True if the email address is valid and registered, False otherwise.
        """
        res = self._json.check_address(email)
        status = res.get('status')
        return status is not None and int(status) == 2

    def export(self, ds, method='url_quick', protocol='as-is',
               protocol_args=None, filenamefmt=None, n=None, email=None,
               requestor=None, verbose=False):
        """
        Submit a data export request.

        A registered email address is required for data exports. You can
        register your email address for data exports from JSOC at

            http://jsoc.stanford.edu/ajax/register_email.html

        An interactive webinterface and additional information is available
        on the JSOC data export webpage:

            http://jsoc.stanford.edu/ajax/exportdata.html

        Note that export requests that were submitted using the webinterface
        can be accessed using the export_from_id() method.

        Parameters
        ----------
        ds : string
            Data export record set query.
        method : string
            Export method. Supported methods are: 'url_quick', 'url',
            'url-tar', 'ftp' and 'ftp-tar'. Default is 'url_quick'.
        protocol : string
            Export protocol. Supported protocols are: 'as-is', 'fits', 'jpg',
            'mpg' and 'mp4'. Default is 'as-is'.
        protocol_args : dict
            Extra protocol arguments for protocols 'jpg', 'mpg' and 'mp4'.
            Valid arguments are: 'ct', 'scaling', 'min', 'max' and 'size'.
        filenamefmt : string, None or False
            Custom filename format string for exported files. This is ignored
            for 'url_quick'/'as-is' data exports. If set to None (default),
            the format string will be generated using the primekeys of the
            data series. If set to False, the filename format string will be
            omitted in the export request.
        n : int or None
            Limits the number of records requested. For positive values, the
            first n records of the record set are returned, for negative
            values the last abs(n) records. If set to None (default), no limit
            is applied.
        email : string or None
            Registered email address. If email is None (default), the current
            default email address is used, which in this case has to be set
            before calling export() by using the Client.email property.
        requestor : string, None or False
            Export user ID. Default is None, in which case the user name is
            determined from the email address. If set to False, the requestor
            argument will be omitted in the export request.
        verbose : bool
            Print export status messages to stdout.

        Returns
        -------
        result : ExportRequest
        """
        if email is None:
            if self._email is None:
                raise ValueError(
                    'The email argument is required, when no default email '
                    'address was set')
            email = self._email
        if filenamefmt is None:
            sname = _extract_series_name(ds)
            filenamefmt = self._generate_filenamefmt(sname)
        elif filenamefmt is False:
            filenamefmt = None
        d = self._json.exp_request(
            ds, email, method=method, protocol=protocol,
            protocol_args=protocol_args, filenamefmt=filenamefmt,
            n=n, requestor=requestor)
        return ExportRequest(d, client=self, verbose=verbose)

    def export_from_id(self, requestid, verbose=False):
        """
        Create an ExportRequest instance from an already existing requestid.

        Parameters
        ----------
        requestid : string
            Export request ID.
        verbose : bool
            Print export status messages to stdout.

        Returns
        -------
        result : ExportRequest
        """
        return ExportRequest._create_from_id(
            requestid, client=self, verbose=verbose)


def _test_info(c, ds):
    sname = c.series(ds)
    res = []
    skiplist = [r'jsoc.*']
    for sni in sname:
        skipit = False
        print(sni)
        for spat in skiplist:
            if re.match(spat, sni):
                print('** skipping series **')
                skipit = True
                break
        if not skipit:
            res.append(c.info(sni))
    return res
