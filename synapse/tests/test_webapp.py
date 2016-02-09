import requests

import synapse.datamodel as s_datamodel
import synapse.lib.webapp as s_webapp

from synapse.tests.common import *

class Horked(Exception):pass

class Foo:

    def bar(self):
        return 'baz'

    @s_datamodel.parsetypes('int', y='int')
    def addup(self, x, y=0):
        return x + y

    def horked(self):
        raise Horked('you are so horked')

class WebAppTest(SynTest):

    def test_webapp_publish(self):

        # tornado does not support windows (yet)
        self.thisHostMustNot(platform='windows')
        foo = Foo()

        wapp = s_webapp.WebApp()
        wapp.listen(0, host='127.0.0.1')
        wapp.addApiPath('/v1/horked', foo.horked )
        wapp.addApiPath('/v1/addup/([0-9]+)', foo.addup )

        port = wapp.getServBinds()[0][1]
        resp = requests.get('http://127.0.0.1:%d/v1/addup/30?y=40' % port, timeout=1).json()

        self.assertEqual( resp.get('ret'), 70 )
        self.assertEqual( resp.get('status'), 'ok' )

        resp = requests.get('http://127.0.0.1:%d/v1/addup/20' % port, timeout=1).json()

        self.assertEqual( resp.get('ret'), 20 )
        self.assertEqual( resp.get('status'), 'ok' )

        resp = requests.get('http://127.0.0.1:%d/v1/horked' % port, timeout=1).json()

        self.assertEqual( resp.get('err'), 'Horked' )
        self.assertEqual( resp.get('status'), 'err' )

        wapp.fini()