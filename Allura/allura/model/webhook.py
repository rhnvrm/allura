#       Licensed to the Apache Software Foundation (ASF) under one
#       or more contributor license agreements.  See the NOTICE file
#       distributed with this work for additional information
#       regarding copyright ownership.  The ASF licenses this file
#       to you under the Apache License, Version 2.0 (the
#       "License"); you may not use this file except in compliance
#       with the License.  You may obtain a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#       Unless required by applicable law or agreed to in writing,
#       software distributed under the License is distributed on an
#       "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
#       KIND, either express or implied.  See the License for the
#       specific language governing permissions and limitations
#       under the License.

import datetime as dt

from ming.odm import FieldProperty, session
from paste.deploy.converters import asint
from tg import config

from allura.model import Artifact


class Webhook(Artifact):

    class __mongometa__:
        name = 'webhook'
        unique_indexes = [('app_config_id', 'type', 'hook_url')]

    type = FieldProperty(str)
    hook_url = FieldProperty(str)
    secret = FieldProperty(str)
    last_sent = FieldProperty(dt.datetime, if_missing=None)

    def url(self):
        app = self.app_config.load()
        app = app(self.app_config.project, self.app_config)
        return '{}webhooks/{}/{}'.format(app.admin_url, self.type, self._id)

    def enforce_limit(self):
        '''Returns False if limit is reached, otherwise True'''
        if self.last_sent is None:
            return True
        now = dt.datetime.utcnow()
        config_type = self.type.replace('-', '_')
        limit = asint(config.get('webhook.%s.limit' % config_type, 30))
        if (now - self.last_sent) > dt.timedelta(seconds=limit):
            return True
        return False

    def update_limit(self):
        self.last_sent = dt.datetime.utcnow()
        session(self).flush(self)