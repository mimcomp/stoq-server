# -*- coding: utf-8 -*-
# vi:si:et:sw=4:sts=4:ts=4

##
## Copyright (C) 2015 Async Open Source <http://www.async.com.br>
## All rights reserved
##
## This program is free software; you can redistribute it and/or
## modify it under the terms of the GNU Lesser General Public License
## as published by the Free Software Foundation; either version 2
## of the License, or (at your option) any later version.
##
## This program is distributed in the hope that it will be useful,
## but WITHOUT ANY WARRANTY; without even the implied warranty of
## MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
## GNU Lesser General Public License for more details.
##
## You should have received a copy of the GNU Lesser General Public License
## along with this program; if not, write to the Free Software
## Foundation, Inc., or visit: http://www.gnu.org/.
##
## Author(s): Stoq Team <stoq-devel@async.com.br>
##

import base64
import contextlib
import hashlib
import imp
import json
import os
import re
import sys
import urlparse

from duplicity import backend
from duplicity import globals as duplicity_globals
try:
    # This is only available on duplicity <= 0.6
    from duplicity.backend import _ensure_urlparser_initialized, urlparser
except ImportError:
    uses_netloc = backend.uses_netloc
else:
    _ensure_urlparser_initialized()
    uses_netloc = urlparser.uses_netloc
import requests
from stoqlib.api import api
from stoqlib.lib.configparser import get_config
from stoqlib.lib.webservice import WebService

_duplicity_bin = '/usr/bin/duplicity'
_duplicity_main = imp.load_source('main', _duplicity_bin)
# Support both http and https
_webservice_url = re.sub('https?', 'stoq', WebService.API_SERVER)


class StoqBackend(backend.Backend):

    SCHEME = 'stoq'
    TIMEOUT = 60

    def __init__(self, url):
        backend.Backend.__init__(self, url)

        self._hash = os.environ['STOQ_BACKUP_HASH']
        assert self._hash
        self._keyhash = hashlib.sha256(os.environ['PASSPHRASE']).hexdigest()
        assert self._keyhash
        self._backup_id = os.environ.get('STOQ_BACKUP_ID', None)
        self._api_url = 'http://%s:%s' % (url.hostname, url.port or 80)

    #
    #  backend.Backend
    #

    def put(self, source_path, remote_filename=None):
        # If remote_filename is None, duplicity API says source_path
        # filename should be used instead
        remote_filename = remote_filename or source_path.get_filename()
        content = base64.b64encode(source_path.get_data())

        post_data = json.loads(self._do_request(
            'put', filename=remote_filename, size=len(content)))

        # Do the actual post request to s3 using the post_data supplied
        res = requests.post(post_data['url'], timeout=self.TIMEOUT,
                            allow_redirects=True, data=post_data['form_data'],
                            files={'file': content})
        assert res.status_code == 200

    def get(self, remote_filename, local_path):
        url = self._do_request(
            'get', filename=remote_filename)

        with open(local_path.name, 'w') as local_file:
            res = requests.get(url)
            local_file.write(base64.b64decode(res.text))

    def list(self):
        response = self._do_request('list')
        # FIXME: Some versions of the duplicity doesn't allow unicode
        return [f.encode('utf-8') if isinstance(f, unicode) else f for
                f in json.loads(response)]

    def delete(self, remote_filename):
        self._do_request('delete', filename=remote_filename)

    def close(self):
        pass

    #
    #  Private
    #

    def _do_request(self, endpoint, method='GET', files=None, **data):
        url = urlparse.urljoin(self._api_url, 'api/backup/' + endpoint)
        data['hash'] = self._hash
        data['log_id'] = self._backup_id
        data['keyhash'] = self._keyhash

        extra_args = {}
        if method == 'GET':
            extra_args['params'] = data
        elif method == 'POST':
            extra_args['data'] = data
        else:
            raise AssertionError

        res = requests.request(method, url, timeout=self.TIMEOUT,
                               files=files, **extra_args)
        assert res.status_code == 200

        return res.text


# For some reason, duplicity 0.7+ changed its backend api to private members
# This is to support it, they should not causa any problems for 0.6
StoqBackend._put = StoqBackend.put
StoqBackend._get = StoqBackend.get
StoqBackend._list = StoqBackend.list
StoqBackend._delete = StoqBackend.delete
StoqBackend._close = StoqBackend.close

uses_netloc.append(StoqBackend.SCHEME)
backend.register_backend(StoqBackend.SCHEME, StoqBackend)


@contextlib.contextmanager
def _mock_environ():
    old_argv = sys.argv[:]
    while sys.argv:
        sys.argv.pop()
    old_environ = os.environ.copy()

    def _restore_environ():
        while sys.argv:
            sys.argv.pop()
        sys.argv.extend(old_argv)
        os.environ.clear()
        os.environ.update(old_environ)

    backup_key = get_config().get('Backup', 'key')
    if not backup_key:
        _restore_environ()
        raise Exception("No backup key set on configuration file")
    os.environ['PASSPHRASE'] = backup_key

    yield

    _restore_environ()


def status(user_hash=None):
    reload(duplicity_globals)

    with _mock_environ():
        os.environ['STOQ_BACKUP_HASH'] = (user_hash or
                                          api.sysparam.get_string('USER_HASH'))

        sys.argv.extend([_duplicity_bin, 'collection-status', _webservice_url])
        _duplicity_main.main()


def backup(backup_dir, full=False):
    reload(duplicity_globals)

    with _mock_environ():
        user_hash = api.sysparam.get_string('USER_HASH')
        os.environ['STOQ_BACKUP_HASH'] = user_hash

        sys.argv.append(_duplicity_bin)
        if full:
            sys.argv.append('full')

        # Display progress and do a full backup monthly
        sys.argv.extend(['--full-if-older-than', '1M', '--progress',
                         backup_dir, _webservice_url])

        # Tell Stoq Link Admin that you're starting a backup
        start_url = urlparse.urljoin(WebService.API_SERVER, 'api/backup/start')
        response = requests.get(start_url, params={'hash': user_hash})

        # If the server rejects the backup, don't even attempt to proceed. Log
        # which error caused the backup to fail
        if response.status_code != 200:
            raise Exception('ERROR: ' + response.content)

        os.environ['STOQ_BACKUP_ID'] = response.content
        _duplicity_main.main()

        # Tell Stoq Link Admin that the backup has finished
        end_url = urlparse.urljoin(WebService.API_SERVER, 'api/backup/end')
        requests.get(end_url,
                     params={'log_id': response.content, 'hash': user_hash})


def restore(restore_dir, user_hash, time=None):
    reload(duplicity_globals)

    with _mock_environ():
        os.environ['STOQ_BACKUP_HASH'] = user_hash

        sys.argv.extend([_duplicity_bin, 'restore',
                         _webservice_url, restore_dir])
        if time is not None:
            sys.argv.extend(['--time', time])

        _duplicity_main.main()
