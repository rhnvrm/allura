import logging
from contextlib import contextmanager
from threading import local
from random import random

import pylons
from tg.controllers import DecoratedController
from webob import exc, Request

from pyforge.lib.stats import timing, StatsRecord

log = logging.getLogger(__name__)

environ = _environ = None

def on_import():
    global environ, _environ
    environ = _environ = Environ()

class ForgeMiddleware(object):
    '''Middleware responsible for pushing the MagicalC object and setting the
    threadlocal _environ.  This is inner middleware, and must be called from
    within the TGController.__call__ method because it depends on pylons.c and pylons.g'''

    def __init__(self, app):
        from pyforge.lib.app_globals import Globals
        self.app = app
        self.g = Globals()

    def __call__(self, environ, start_response):
        _environ.set_environment(environ)
        magical_c = MagicalC(pylons.c._current_obj(), environ)
        pylons.c._push_object(magical_c)
        try:
            result = self.app(environ, start_response)
            if isinstance(result, list):
                self._cleanup_request(environ)
                return result
            else:
                return self._cleanup_iterator(result, environ)
        finally:
            pylons.c._pop_object()

    def _cleanup_request(self, environ):
        for msg in environ.get('allura.queued_messages', []):
            self.g._publish(**msg)
        carrot = environ.pop('allura.carrot.connection', None)
        if carrot: carrot.close()
        _environ.set_environment({})

    def _cleanup_iterator(self, result, environ):
        for x in result:
            yield x
        self._cleanup_request(environ)

class SfxLoginMiddleware(object):

    def __init__(self, app, config):
        from sf.phpsession import SFXSessionMgr
        self.app = app
        self.config = config
        self.sfx_session_mgr = SFXSessionMgr()
        self.sfx_session_mgr.setup_sessiondb_connection_pool(config)

    def __call__(self, environ, start_response):
        request = Request(environ)
        try:
            self.handle(request)
        except exc.HTTPException, resp:
            return resp(environ, start_response)
        resp = request.get_response(self.app)
        return resp(environ, start_response)

    def handle(self, request):
        session = request.environ['beaker.session']
        request.environ['allura.sfx_session_manager'] = self.sfx_session_mgr

class SSLMiddleware(object):
    'Verify the https/http schema is correct'

    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        req = Request(environ)
        resp = None
        try:
            request_uri = req.url
            request_uri.decode('ascii')
        except UnicodeError:
            resp = exc.HTTPNotFound()
        secure = req.environ.get('HTTP_X_SFINC_SSL', 'false') == 'true'
        srv_path = req.path_url.split('://', 1)[-1]
        if req.cookies.get('SFUSER'):
            if not secure:
                resp = exc.HTTPFound(location='https://' + srv_path)
        elif secure:
            resp = exc.HTTPFound(location='http://' + srv_path)

        if resp is None:
            resp = req.get_response(self.app)
        return resp(environ, start_response)

class StatsMiddleware(object):

    def __init__(self, app, config):
        self.app = app
        self.config = config
        self.log = logging.getLogger('stats')
        self.active = False
        try:
            self.sample_rate = config.get('stats.sample_rate', 0.25)
            self.instrument_pymongo()
            self.instrument_template()
            self.active = True
        except KeyError:
            self.sample_rate = 0

    def instrument_pymongo(self):
        import pymongo.collection
        import ming.orm
        timing('mongo').decorate(pymongo.collection.Collection,
                                 'count find find_one')
        timing('ming').decorate(ming.orm.ormsession.ORMSession,
                                'flush find get')
        timing('ming').decorate(ming.orm.ormsession.ORMCursor,
                                'next')

    def instrument_template(self):
        import genshi.template
        timing('template').decorate(genshi.template.Template,
                                    '_prepare _parse generate')
        timing('render').decorate(genshi.Stream,
                                  'render')


    def __call__(self, environ, start_response):
        req = Request(environ)
        req.environ['sf.stats'] = s = StatsRecord(req, random() < self.sample_rate)
        with s.timing('total'):
            resp = req.get_response(self.app)
            result = resp(environ, start_response)
        if s.active:
            self.log.info('Stats: %r', s)
        return result

class Environ(object):
    _local = local()

    def set_environment(self, environ):
        self._local.environ = environ

    def __getitem__(self, name):
        if not hasattr(self._local, 'environ'):
            self.set_environment({})
        try:
            return self._local.environ[name]
        except AttributeError:
            self._local.environ = {}
            raise KeyError, name

    def __setitem__(self, name, value):
        if not hasattr(self._local, 'environ'):
            self.set_environment({})
        try:
            self._local.environ[name] = value
        except AttributeError:
            self._local.environ = {name:value}

    def __delitem__(self, name):
        if not hasattr(self._local, 'environ'):
            self.set_environment({})
        try:
            del self._local.environ[name]
        except AttributeError:
            self._local.environ = {}
            raise KeyError, name

    def __getattr__(self, name):
        if not hasattr(self._local, 'environ'):
            self.set_environment({})
        return getattr(self._local.environ, name)

    def __repr__(self):
        if not hasattr(self._local, 'environ'):
            self.set_environment({})
        return repr(self._local.environ)

    def __contains__(self, key):
        return self._local.environ and key in self._local.environ

class MagicalC(object):
    '''Magically saves various attributes to the environ'''
    _saved_attrs = set(['project', 'app', 'queued_messages'])

    def __init__(self, old_c, environ):
        self._old_c = old_c
        self._environ = environ

    def __getattr__(self, name):
        return getattr(self._old_c, name)

    def __setattr__(self, name, value):
        if name in MagicalC._saved_attrs:
            self._environ['allura.' + name] = value
        if name not in ('_old_c', '_environ'):
            setattr(self._old_c, name, value)
        object.__setattr__(self, name, value)

    def __delattr__(self, name):
        if name not in ('_old_c', '_environ'):
            delattr(self._old_c, name)
        object.__delattr__(self, name)

@contextmanager
def fake_pylons_context(request):
    from pyforge.lib.app_globals import Globals
    class EmptyClass(object): pass
    pylons.c._push_object(MagicalC(EmptyClass(), environ))
    pylons.g._push_object(Globals())
    pylons.request._push_object(request)
    try:
        yield
    finally:
        pylons.c._pop_object()
        pylons.g._pop_object()
        pylons.request._pop_object()

on_import()
