"""Exercise actual route functions without importing live DB or AI clients."""
import ast
import calendar
import hmac
import secrets
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import patch
import pytz
from flask import Flask, render_template, request, redirect, url_for, jsonify, session, abort

ROOT = Path(__file__).resolve().parents[1]
source = ast.parse((ROOT / 'app.py').read_text(encoding='utf-8'))
names = {'resolve_student','protect_admin','private_admin_response','admin_login','admin_logout',
         'student_timezone','filter_recent_logs','home','index','edit','save','admin_page','update_log',
         'delete_log','get_older_logs'}
app = Flask(__name__, template_folder=str(ROOT / 'templates'))
app.config.update(TESTING=True, SECRET_KEY='verification-only-key', ADMIN_PASSWORD='verification-only-password')
ns = dict(globals())
for node in source.body:
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == 'STUDENT_MAP':
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'app.py', 'exec'), ns)
    if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in {'STUDENT_MAP','JST','PST','FRISCO'} for t in node.targets):
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'app.py', 'exec'), ns)
    if isinstance(node, ast.FunctionDef) and node.name in names:
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'app.py', 'exec'), ns)

class Store:
    writes = []
    reads = 0
    def collection(self, name): return self
    def document(self, name): self.key = name; return self
    def stream(self):
        self.reads += 1
        return [type('Doc', (), {'id': row['date'], 'to_dict': lambda self, r=row: r})() for row in records]
    def set(self, data, merge=False): self.writes.append(('set', self.key, data))
    def delete(self): self.writes.append(('delete', self.key))
store = Store()
now = datetime.now(pytz.timezone('America/Chicago'))
records = [dict(date=(now-timedelta(days=i)).strftime('%Y-%m-%d'), stamp='🍪',
                public_comment='練習用の記録です。'+('長い文章です。'*24), student_comment='読めた！',
                lesson_point='架空の教材を読みました。', homework='https://example.com/'+('sample/'*50),
                ai_memo='検証用メモ', category='日常') for i in range(100)]
ns.update(db=store, get_logs=lambda student, **kw: sorted(records, key=lambda r:r['date'],reverse=True),
          get_candy_count=lambda student:0)

class MinorChangeTests(unittest.TestCase):
    def setUp(self):
        self.client=app.test_client()
        store.writes=[]
        app.config['ADMIN_PASSWORD']='verification-only-password'
    def login(self):
        self.client.get('/admin/login?student=ann')
        with self.client.session_transaction() as s: token=s['login_csrf']
        result=self.client.post('/admin/login?student=ann',data={'password':'verification-only-password','csrf_token':token})
        self.assertEqual(result.status_code,302)
        with self.client.session_transaction() as s: return s['admin_csrf']
    def test_locked_routes(self):
        for path, method in [('/admin/ann','get'),('/admin/taisei','get'),('/admin/s01','get'),('/update/ann/2026-01-01','post'),('/delete/ann/2026-01-01','post')]:
            self.assertEqual(getattr(self.client,method)(path).status_code,302)
        self.assertEqual(store.writes,[])
    def test_wrong_password_and_missing_configuration(self):
        self.client.get('/admin/login')
        with self.client.session_transaction() as s: token=s['login_csrf']
        r=self.client.post('/admin/login',data={'password':'wrong','csrf_token':token})
        self.assertIn('パスワードが違います',r.get_data(as_text=True))
        self.assertEqual(self.client.get('/admin/ann?pwd=admin123').status_code,302)
        app.config['ADMIN_PASSWORD']=''
        self.assertEqual(self.client.get('/admin/login').status_code,503)
    def test_login_switch_update_delete_and_logout(self):
        token=self.login()
        for path in ['/admin/ann','/admin/taisei','/admin/s01']:
            r=self.client.get(path); self.assertEqual(r.status_code,200)
            self.assertIn('<dialog',r.get_data(as_text=True))
            self.assertEqual(r.headers['Cache-Control'],'no-store')
        self.assertEqual(self.client.post('/update/s01/2026-01-01',data={'csrf_token':token,'public_comment':'updated'}).status_code,302)
        self.assertEqual(store.writes[-1][0],'set')
        self.assertEqual(self.client.get('/delete/ann/2026-01-01').status_code,405)
        self.assertEqual(self.client.post('/delete/ann/2026-01-01',data={'csrf_token':token}).status_code,302)
        self.assertEqual(store.writes[-1][0],'delete')
        self.client.post('/admin/logout',data={'csrf_token':token})
        self.assertEqual(self.client.get('/admin/ann').status_code,302)
    def test_missing_csrf_cannot_write(self):
        self.login()
        self.assertEqual(self.client.post('/update/ann/2026-01-01').status_code,400)
        self.assertEqual(self.client.post('/delete/ann/2026-01-01').status_code,400)
        self.assertEqual(store.writes,[])
    def test_both_public_pages_and_pagination(self):
        for mode in ['s01','s02']:
            r=self.client.get('/'+mode); self.assertEqual(r.status_code,200)
            html=r.get_data(as_text=True)
            self.assertIn('See more',html); self.assertIn('Next Cookie',html)
            self.assertNotIn('olderOffset',html)
            if mode=='s01': self.assertIn('FRISCO',html)
            first=self.client.get('/api/logs/older/'+mode+'?offset=0&limit=10').json
            second=self.client.get('/api/logs/older/'+mode+'?offset=10&limit=10').json
            self.assertEqual(len(first['logs']),10)
            self.assertFalse({r['date'] for r in first['logs']} & {r['date'] for r in second['logs']})
            self.assertEqual(first['logs'],sorted(first['logs'],key=lambda r:r['date'],reverse=True))
        self.assertEqual(self.client.get('/api/logs/older/s01?offset=no').status_code,400)
    def test_calendar_cutoff_and_dst(self):
        class Clock:
            @classmethod
            def now(cls,tz): return tz.localize(datetime(2027,4,30,12))
        with patch.dict(ns,datetime=Clock):
            recent,older=ns['filter_recent_logs']([{'date':'2027-02-27'},{'date':'2027-02-28'}],student_name='ann')
            self.assertEqual([r['date'] for r in recent],['2027-02-28'])
            self.assertEqual(len(older),1)
        zone=ns['student_timezone']('ann')
        self.assertEqual(zone.localize(datetime(2027,1,15)).tzname(),'CST')
        self.assertEqual(zone.localize(datetime(2027,7,15)).tzname(),'CDT')

if __name__=='__main__':
    import sys
    if '--preview' in sys.argv:
        app.run(host='127.0.0.1',port=8765,debug=False)
    else: unittest.main()
