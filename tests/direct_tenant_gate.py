"""Real HTTP gate: two users, same title, isolated cached sources; no media fetches."""
import concurrent.futures
import importlib.util
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler

spec = importlib.util.spec_from_file_location('provider', Path(__file__).parent/'fixtures/direct_provider.py')
provider = importlib.util.module_from_spec(spec)
spec.loader.exec_module(provider)

class Fixture(provider.Handler):
    def do_GET(self):
        self.tenant = self.path.split('/')[1] if self.path.startswith(('/a/', '/b/', '/empty/')) else None
        if self.tenant:
            self.path = self.path[len(self.tenant)+1:]
        super().do_GET()

    def respond(self, status, body):
        if self.tenant and 'streams' in body:
            body = {'streams': [] if self.tenant == 'empty' else [{
                'name': f'Private tenant {self.tenant}',
                'url': f'https://tenant-{self.tenant}.example/private-{self.tenant}.mp4',
                'behaviorHints': {'filename': f'Tenant.{self.tenant}.1080p.H264.mp4'},
            }]}
        super().respond(status, body)

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_):
        return None


def main():
    fixture = ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
    threading.Thread(target=fixture.serve_forever, daemon=True).start()
    fixture_url = f'http://127.0.0.1:{fixture.server_port}'
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    base = f'http://127.0.0.1:{port}'
    def request(path, body=None, token=None, device='admin'):
        headers={'x-emby-authorization':f'MediaBrowser Client="TenantGate", Device="test", DeviceId="{device}", Version="1"'+(f', Token="{token}"' if token else '')}
        if body is not None:
            headers['content-type']='application/json'
        req=Request(base+path,data=json.dumps(body).encode() if body is not None else None,headers=headers)
        try:
            response=build_opener(NoRedirect).open(req,timeout=30)
        except HTTPError as e:
            response=e
        raw=response.read(2*1024*1024)
        try:
            data=json.loads(raw) if raw else None
        except ValueError:
            data=raw.decode(errors='replace')
        return response.status,data,dict(response.headers)
    def ok(*args, **kwargs):
        status,data,_=request(*args,**kwargs)
        assert 200<=status<300,(args[0],status,str(data)[:250])
        return data
    with tempfile.TemporaryDirectory(prefix='remux-tenant-gate-') as directory:
        root=Path(directory)
        denied=root/'deny.sh'
        denied.write_text('#!/bin/sh\necho invoked >> "'+str(root/'media-attempts')+'"\nexit 23\n')
        denied.chmod(0o700)
        env=dict(os.environ,DIRECT_ONLY='true',TENANT_STREAMS='true',DISABLE_DHT='true',DISABLE_STARTUP_TASKS='true',DATA_DIR=directory,DATABASE_URL=f'sqlite://{root}/db.sqlite?mode=rwc',HOST='127.0.0.1',PORT=str(port),TMDB_BASE_URL=fixture_url+'/tmdb',FFPROBE_PATH=str(denied),FFMPEG_PATH=str(denied),RUST_LOG='warn')
        with (root/'server.log').open('w') as logs:
            server=subprocess.Popen([str(Path(sys.argv[1]).resolve())],env=env,cwd=root,stdout=logs,stderr=logs)
            try:
                for _ in range(100):
                    try:
                        status,_,headers=request('/system/info/public')
                        if status==200:
                            assert headers.get('x-tuvora-tenant-streams')=='1','tenant isolation flag missing'
                            break
                    except URLError:
                        pass
                    time.sleep(.2)
                else:
                    raise RuntimeError('Server not ready: '+(root/'server.log').read_text()[-1000:])
                password=secrets.token_urlsafe(24)
                ok('/startup/configuration',{'ServerName':'Tenant isolation test'})
                ok('/startup/user',{'Name':'admin','Password':password})
                ok('/startup/complete',{})
                admin=ok('/users/authenticatebyname',{'Username':'admin','Pw':password})['AccessToken']
                config=ok('/system/configuration',token=admin)
                config.update({'P2pEnabled':False,'RemuxdbEnabled':False})
                ok('/system/configuration',config,token=admin)
                for addon in ok('/addons',token=admin):
                    ok('/addons/'+addon['id'],{'enabled':False},token=admin)
                users={}
                addons={}
                for key in ('a','b','empty'):
                    addon=ok('/addons',{'preset':{'kind':'stremio','config':{'manifest_url':fixture_url+'/'+key+'/manifest.json'}},'name':'bridge-'+key,'isDefault':False},token=admin)
                    addons[key]=addon['id']
                    ok('/addons/'+addon['id'],{'enabled':True,'httpRedirectStream':True,'probeOnScan':False,'subtitleExtraction':False},token=admin)
                    if key!='empty':
                        user=ok('/users/new',{'Name':'tenant-'+key,'Password':password},token=admin)
                        ok('/users/'+user['Id']+'/addons',[addon['id']],token=admin)
                        users[key]=ok('/users/authenticatebyname',{'Username':'tenant-'+key,'Pw':password},device=key+'-phone')
                a,b=users['a'],users['b']
                # A fresh lazy library has no cached titles yet. It must still be
                # discoverable by ordinary Jellyfin clients before the first search.
                views=ok('/users/'+a['User']['Id']+'/views',token=a['AccessToken'],device='a-phone')['Items']
                libraries={view.get('CollectionType'):view for view in views}
                assert 'movies' in libraries and 'tvshows' in libraries, ('Fresh virtual libraries hidden',[(v.get('Name'),v.get('CollectionType')) for v in views])
                items=ok('/items?'+urlencode({'SearchTerm':'Synthetic POC','IncludeItemTypes':'Movie','Recursive':'true','ParentId':libraries['movies']['Id']}),token=a['AccessToken'],device='a-phone')['Items']
                assert items,'No synthetic movie'
                movie=items[0]['Id']
                wrong_kind=ok('/items?'+urlencode({'SearchTerm':'Synthetic POC','IncludeItemTypes':'Series','Recursive':'true','ParentId':libraries['movies']['Id']}),token=a['AccessToken'],device='a-phone')['Items']
                assert not wrong_kind,'TV series escaped into the movie library search'
                default_kinds=ok('/items?'+urlencode({'SearchTerm':'Synthetic POC','Recursive':'true','ParentId':libraries['movies']['Id']}),token=a['AccessToken'],device='a-phone')['Items']
                assert default_kinds and all(item['Type']=='Movie' for item in default_kinds),'Untyped search escaped movie scope'
                wrong_movie=ok('/items?'+urlencode({'SearchTerm':'Synthetic POC','IncludeItemTypes':'Movie','Recursive':'true','ParentId':libraries['tvshows']['Id']}),token=a['AccessToken'],device='a-phone')['Items']
                assert not wrong_movie,'Movie escaped into TV library search'
                shows=ok('/items?'+urlencode({'SearchTerm':'Synthetic POC','IncludeItemTypes':'Series','Recursive':'true','ParentId':libraries['tvshows']['Id']}),token=a['AccessToken'],device='a-phone')['Items']
                assert shows,'No synthetic series through TV library'
                episodes=ok('/shows/'+shows[0]['Id']+'/episodes?'+urlencode({'UserId':a['User']['Id'],'Season':1}),token=a['AccessToken'],device='a-phone')['Items']
                episode=next((e for e in episodes if e.get('ParentIndexNumber')==1 and e.get('IndexNumber')==1),None)
                assert episode,'Series did not resolve season 1 episode 1'
                episode_info=ok('/items/'+episode['Id']+'/playbackinfo',{'UserId':a['User']['Id'],'IsPlayback':True},token=a['AccessToken'],device='a-phone')
                assert 'tenant-a.example' in json.dumps(episode_info),'Episode has no direct source'
                assert 'tenant-b.example' not in json.dumps(episode_info),'Foreign episode source'
                if len(sys.argv)>2:
                    # Synthetic wire evidence for client deserialization/lookup tests.
                    Path(sys.argv[2]).write_text(json.dumps({'views':{'Items':views},'movie':{'Items':items},'series':{'Items':shows},'episodes':{'Items':episodes},'episodePlayback':episode_info}))
                def playback(key):
                    session=users[key]
                    body=ok('/items/'+movie+'/playbackinfo',{'UserId':session['User']['Id'],'IsPlayback':True},token=session['AccessToken'],device=key+'-phone')
                    serialized=json.dumps(body)
                    assert f'tenant-{key}.example' in serialized,serialized[:200]
                    other='b' if key=='a' else 'a'
                    assert f'tenant-{other}.example' not in serialized,'Cross-tenant PlaybackInfo leak'
                    return body
                pa=playback('a');pb=playback('b');playback('a')
                # Jellyfin compatibility may alias the first source Id to the public title Id.
                # Validate the actual persisted private source IDs, including a forged foreign ID.
                with sqlite3.connect('file:'+str(root/'db.sqlite')+'?mode=ro',uri=True) as db:
                    rows=db.execute("select lower(hex(id)), lower(hex(user_id)) from media where kind='stream'").fetchall()
                source_sets={key:{source for source,owner in rows if owner==users[key]['User']['Id'].replace('-','').lower()} for key in ('a','b')}
                assert source_sets['a'] and source_sets['b'],'Stream owner missing from storage'
                assert source_sets['a'].isdisjoint(source_sets['b']),'Shared private stream IDs'
                source_b=next(iter(source_sets['b']))
                status,_,headers=request('/videos/'+movie+'/stream.mp4?'+urlencode({'Static':'true','MediaSourceId':source_b}),token=a['AccessToken'],device='a-phone')
                assert status>=400,'Cross-tenant source ID was accepted'
                status,data,_=request('/items/'+source_b+'?Fields=MediaSources',token=a['AccessToken'],device='a-phone')
                assert status>=400 or data is None,'Cross-tenant stream detail accepted'
                listed=ok('/items?IncludeItemTypes=Stream&Recursive=true&Fields=MediaSources',token=a['AccessToken'],device='a-phone')
                assert 'tenant-b.example' not in json.dumps(listed),'Cross-tenant listing leak'
                for _ in range(3):
                    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                        list(pool.map(playback,('a','b')))
                detail=ok('/items/'+movie+'?Fields=MediaSources',token=a['AccessToken'],device='a-phone')
                assert 'tenant-b.example' not in json.dumps(detail),'Cross-tenant item source leak'
                ok('/users/'+b['User']['Id']+'/addons',[addons['empty']],token=admin)
                status,data,_=request('/items/'+movie+'/playbackinfo',{'UserId':b['User']['Id']},token=b['AccessToken'],device='b-phone')
                assert status>=400 or not data.get('MediaSources'),'Empty addon reused stale/foreign streams'
                playback('a')
                assert not (root/'media-attempts').exists(),'Media process invoked'
                assert not any(e['path'].startswith('/media/') for e in provider.EVENTS),'Media origin requested'
                print(json.dumps({'passed':True,'checks':['fresh Movies/Shows discovery','movie lookup scoped to library','contradictory and untyped searches respect library kind','series to episode to direct playback metadata','same-title A/B/A cache isolation','distinct tenant stream IDs','foreign source ID denied','foreign source detail denied','foreign source listing scrubbed','parallel playback metadata isolated','item MediaSources isolated','empty addon cannot reuse cached streams','other user survives addon changes'],'media_origin_requests':0,'media_process_attempts':0}))
            finally:
                server.terminate()
                try:
                    server.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    server.kill();server.wait()
                fixture.shutdown()

if __name__=='__main__':
    main()
