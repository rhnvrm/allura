import unittest

from pylons import g, c

from ming.orm import ThreadLocalORMSession

from pyforge.tests import helpers
from pyforge import model as M
from pyforge.lib import helpers as h
from forgewiki import model as WM

class TestNotification(unittest.TestCase):

    def setUp(self):
        helpers.setup_basic_test()
        helpers.setup_global_objects()
        _clear_subscriptions()
        ThreadLocalORMSession.flush_all()
        ThreadLocalORMSession.close_all()

    def test_subscribe_unsubscribe(self):
        M.Mailbox.subscribe(type='direct')
        ThreadLocalORMSession.flush_all()
        ThreadLocalORMSession.close_all()
        subscriptions = M.Mailbox.query.find(dict(
            project_id=c.project._id,
            app_config_id=c.app.config._id,
            user_id=c.user._id)).all()
        assert len(subscriptions) == 1
        assert subscriptions[0].type=='direct'
        assert M.Mailbox.query.find().count() == 1
        M.Mailbox.unsubscribe()
        ThreadLocalORMSession.flush_all()
        ThreadLocalORMSession.close_all()
        subscriptions = M.Mailbox.query.find(dict(
            project_id=c.project._id,
            app_config_id=c.app.config._id,
            user_id=c.user._id)).all()
        assert len(subscriptions) == 0
        assert M.Mailbox.query.find().count() == 0

class TestPostNotifications(unittest.TestCase):

    def setUp(self):
        helpers.setup_basic_test()
        helpers.setup_global_objects()
        g.set_app('wiki')
        _clear_subscriptions()
        ThreadLocalORMSession.flush_all()
        ThreadLocalORMSession.close_all()
        self.pg = WM.Page.query.get()

    def test_post_notification(self):
        self._post_notification()
        queue = g.mock_amq.exchanges['react']
        assert len(queue) == 1
        assert queue[0]['message']['artifact_index_id'] == self.pg.index()['id']

    def test_post_user_notification(self):
        u = M.User.query.get(username='test-admin')
        n = M.Notification.post_user(u, self.pg, 'metadata')
        ThreadLocalORMSession.flush_all()
        ThreadLocalORMSession.close_all()
        flash_msgs = list(h.pop_user_notifications(u))
        assert len(flash_msgs) == 1, flash_msgs
        msg = flash_msgs[0]
        assert msg['text'].startswith('WikiPage Home modified by Test Admin')
        assert msg['subject'].startswith('[test:wiki]')
        flash_msgs = list(h.pop_user_notifications(u))
        assert not flash_msgs, flash_msgs

    def test_delivery(self):
        g.mock_amq.setup_handlers()
        self._subscribe()
        self._post_notification()
        g.mock_amq.handle('react')# should deliver msg to mailbox
        mbox = M.Mailbox.query.get()
        assert len(mbox.queue) == 1

    def test_email(self):
        g.mock_amq.setup_handlers()
        self._subscribe()
        self._post_notification()
        g.mock_amq.handle('react')# should deliver msg to mailbox
        M.Mailbox.fire_ready()
        assert len(g.mock_amq.exchanges['audit']) == 1
        msg = g.mock_amq.exchanges['audit'][0]['message']
        assert msg['text'].startswith('WikiPage Home modified by Test Admin')
        assert 'you indicated interest in ' in msg['text']

    def _subscribe(self):
        self.pg.subscribe(type='direct')
        ThreadLocalORMSession.flush_all()
        ThreadLocalORMSession.close_all()

    def _post_notification(self):
        return M.Notification.post(self.pg, 'metadata')

class TestSubscriptionTypes(unittest.TestCase):

    def setUp(self):
        helpers.setup_basic_test()
        helpers.setup_global_objects()
        g.set_app('wiki')
        _clear_subscriptions()
        ThreadLocalORMSession.flush_all()
        ThreadLocalORMSession.close_all()
        self.pg = WM.Page.query.get()
        g.mock_amq.setup_handlers()

    def test_direct_sub(self):
        self._subscribe()
        self._post_notification()
        self._post_notification()
        g.mock_amq.handle('react')
        g.mock_amq.handle('react')
        M.Mailbox.fire_ready()
        assert len(g.mock_amq.exchanges['audit']) == 2

    def test_digest_sub(self):
        self._subscribe('digest')
        self._post_notification(text='x'*1024)
        self._post_notification()
        g.mock_amq.handle('react')
        g.mock_amq.handle('react')
        M.Mailbox.fire_ready()
        assert len(g.mock_amq.exchanges['audit']) == 1
        assert len(g.mock_amq.exchanges['audit'][0]['message']['text']) > 1024

    def test_summary_sub(self):
        self._subscribe('summary')
        self._post_notification(text='x'*1024)
        self._post_notification()
        g.mock_amq.handle('react')
        g.mock_amq.handle('react')
        M.Mailbox.fire_ready()
        assert len(g.mock_amq.exchanges['audit']) == 1
        assert len(g.mock_amq.exchanges['audit'][0]['message']['text']) < 1024

    def test_message(self):
        self._subscribe()
        thd = M.Thread.query.get(artifact_reference=self.pg.dump_ref())
        p = thd.post('This is a very cool message')
        g.mock_amq.handle_all()
        M.Mailbox.fire_ready()
        ThreadLocalORMSession.flush_all()
        ThreadLocalORMSession.close_all()
        assert len(g.mock_amq.exchanges['audit']) == 1
        msg = g.mock_amq.exchanges['audit'][0]['message']
        assert 'Home@wiki.test.p' in msg['reply_to']
        assert 'Test Admin' in msg['from']

    def _clear_subscriptions(self):
        M.Mailbox.query.remove({})
        ThreadLocalORMSession.flush_all()
        ThreadLocalORMSession.close_all()

    def _subscribe(self, type='direct', topic=None):
        self.pg.subscribe(type=type, topic=topic)
        ThreadLocalORMSession.flush_all()
        ThreadLocalORMSession.close_all()

    def _post_notification(self, text=None):
        return M.Notification.post(self.pg, 'metadata', text=text)

def _clear_subscriptions():
        M.Mailbox.query.remove({})


