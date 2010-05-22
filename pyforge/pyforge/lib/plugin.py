'''
Pyforge plugins for authentication and project registration
'''
import logging

from random import randint
from hashlib import sha256
from base64 import b64encode
from datetime import datetime

import ldap
import pkg_resources
from tg import config
from pylons import g, c
from webob import exc

from ming.utils import LazyProperty
from ming.orm import session
from ming.orm import ThreadLocalORMSession

from pyforge.lib import helpers as h

log = logging.getLogger(__name__)

class AuthenticationProvider(object):

    def __init__(self, request):
        self.request = request

    @classmethod
    def get(cls, request):
        method = config.get('auth.method', 'local')
        for ep in pkg_resources.iter_entry_points('pyforge.auth', method):
            return ep.load()(request)
        return None

    @LazyProperty
    def session(self):
        return self.request.environ['beaker.session']

    def authenticate_request(self):
        from pyforge import model as M
        return M.User.query.get(_id=self.session.get('userid', None))

    def register_user(self, user_doc):
        raise NotImplementedError, 'register_user'

    def _login(self):
        raise NotImplementedError, '_login'

    def login(self, user=None):
        try:
            if user is None: user = self._login()
            self.session['userid'] = user._id
            self.session.save()
            return user
        except exc.HTTPUnauthorized:
            self.logout()
            raise

    def logout(self):
        self.session['userid'] = None
        self.session.save()

    def by_username(self, username):
        raise NotImplementedError, 'by_username'

    def set_password(self, user, old_password, new_password):
        raise NotImplementedError, 'set_password'

class LocalAuthenticationProvider(AuthenticationProvider):

    def register_user(self, user_doc):
        from pyforge import model as M
        return M.User(**user_doc)

    def _login(self):
        user = self.by_username(self.request.params['username'])
        if not self._validate_password(user, self.request.params['password']):
            raise exc.HTTPUnauthorized()
        return user

    def _validate_password(self, user, password):
        if user is None: return False
        if not user.password: return False
        salt = str(user.password[6:6+user.SALT_LEN])
        check = self._encode_password(password, salt)
        if check != user.password: return False
        return True

    def by_username(self, username):
        from pyforge import model as M
        return M.User.query.get(username=username)

    def set_password(self, user, old_password, new_password):
        user.password = self._encode_password(new_password)

    def _encode_password(self, password, salt=None):
        from pyforge import model as M
        if salt is None:
            salt = ''.join(chr(randint(1, 0x7f))
                           for i in xrange(M.User.SALT_LEN))
        hashpass = sha256(salt + password.encode('utf-8')).digest()
        return 'sha256' + salt + b64encode(hashpass)

class LdapAuthenticationProvider(AuthenticationProvider):

    def register_user(self, user_doc):
        from pyforge import model as M
        password = user_doc.pop('password', None)
        result = M.User(**user_doc)
        dn = 'uid=%s,%s' % (user_doc['username'], config['auth.ldap.suffix'])
        try:
            con = ldap.initialize(config['auth.ldap.server'])
            con.bind_s(config['auth.ldap.admin_dn'],
                       config['auth.ldap.admin_password'])
            ldap_info = dict(
                uid=user_doc['username'],
                displayName=user_doc['display_name'],
                cn=user_doc['display_name'],
                userPassword=password,
                objectClass=['inetOrgPerson'],
                givenName=user_doc['display_name'].split()[0],
                sn=user_doc['display_name'].split()[-1])
            ldap_info = dict((k,v) for k,v in ldap_info.iteritems()
                             if v is not None)
            try:
                con.add_s(dn, ldap_info.items())
            except ldap.ALREADY_EXISTS:
                con.modify_s(dn, [(ldap.MOD_REPLACE, k, v)
                                  for k,v in ldap_info.iteritems()])
            con.unbind_s()
        except:
            raise
        return result

    def by_username(self, username):
        from pyforge import model as M
        return M.User.query.get(username=username)

    def set_password(self, user, old_password, new_password):
        try:
            dn = 'uid=%s,%s' % (self.username, config['auth.ldap.suffix'])
            con = ldap.initialize(config['auth.ldap.server'])
            con.bind_s(dn, old_password)
            con.modify_s(dn, [(ldap.MOD_REPLACE, 'userPassword', new_password)])
            con.unbind_s()
        except ldap.INVALID_CREDENTIALS:
            raise exc.HTTPUnauthorized()

    def _login(self):
        from pyforge import model as M
        user = M.User.query.get(username=self.request.params['username'])
        if user is None: raise exc.HTTPUnauthorized()
        try:
            dn = 'uid=%s,%s' % (user.username, config['auth.ldap.suffix'])
            con = ldap.initialize(config['auth.ldap.server'])
            con.bind_s(dn, self.request.params['password'])
            con.unbind_s()
        except ldap.INVALID_CREDENTIALS:
            raise exc.HTTPUnauthorized()
        return user

class ProjectRegistrationProvider(object):

    @classmethod
    def get(cls):
        method = config.get('registration.method', 'local')
        for ep in pkg_resources.iter_entry_points('pyforge.project_registration', method):
            return ep.load()()
        return None

    def register_project(self, neighborhood, shortname, user, user_project):
        '''Register a new project in the neighborhood.  The given user will
        become the project's superuser.  If no user is specified, c.user is used.
        '''
        from pyforge import model as M
        assert h.re_path_portion.match(shortname.replace('/', '')), \
            'Invalid project shortname'
        p = M.Project.query.get(shortname=shortname)
        if p:
            assert p.neighborhood == neighborhood, (
                'Project %s exists in neighborhood %s' % (
                    shortname, p.neighborhood.name))
            return p
        database = 'project:' + shortname.replace('/', ':').replace(' ', '_')
        p = M.Project(neighborhood_id=neighborhood._id,
                    shortname=shortname,
                    name=shortname,
                    short_description='',
                    description=(shortname + '\n'
                                 + '=' * 80 + '\n\n'
                                 + 'You can edit this description in the admin page'),
                    database=database,
                    last_updated = datetime.utcnow(),
                    is_root=True)
        try:
            p.configure_project_database()
            with h.push_config(c, project=p, user=user):
                assert M.ProjectRole.query.find().count() == 0, \
                    'Project roles already exist'
                # Install default named roles (#78)
                role_owner = M.ProjectRole(name='Admin')
                role_developer = M.ProjectRole(name='Developer')
                role_member = M.ProjectRole(name='Member')
                role_auth = M.ProjectRole(name='*authenticated')
                role_anon = M.ProjectRole(name='*anonymous')
                # Setup subroles
                role_owner.roles = [ role_developer._id ]
                role_developer.roles = [ role_member._id ]
                p.acl['create'] = [ role_owner._id ]
                p.acl['read'] = [ role_owner._id, role_developer._id, role_member._id,
                                  role_anon._id ]
                p.acl['update'] = [ role_owner._id ]
                p.acl['delete'] = [ role_owner._id ]
                p.acl['tool'] = [ role_owner._id ]
                p.acl['security'] = [ role_owner._id ]
                pr = user.project_role()
                pr.roles = [ role_owner._id, role_developer._id, role_member._id ]
                # Setup builtin tool applications
                if user_project:
                    p.install_app('profile', 'profile')
                else:
                    p.install_app('home', 'home')
                p.install_app('admin', 'admin')
                p.install_app('search', 'search')
                ThreadLocalORMSession.flush_all()
        except:
            ThreadLocalORMSession.close_all()
            log.exception('Error registering project, attempting to drop %s',
                          database)
            try:
                session(p).impl.bind._conn.drop_database(database)
            except:
                log.exception('Error dropping database %s', database)
                pass
            raise
        g.publish('react', 'forge.project_created')
        return p

    def register_subproject(self, project, name, user, install_apps):
        from pyforge import model as M
        assert h.re_path_portion.match(name), 'Invalid subproject shortname'
        shortname = project.shortname + '/' + name
        sp = M.Project(
            parent_id=project._id,
            neighborhood_id=project.neighborhood_id,
            shortname=shortname,
            name=name,
            database=project.database,
            last_updated = datetime.utcnow(),
            is_root=False)
        with h.push_config(c, project=sp):
            M.AppConfig.query.remove(dict(project_id=c.project._id))
            if install_apps:
                sp.install_app('home', 'home')
                sp.install_app('admin', 'admin')
                sp.install_app('search', 'search')
            g.publish('react', 'forge.project_created')
        return sp


class LocalProjectRegistrationProvider(ProjectRegistrationProvider):
    pass
