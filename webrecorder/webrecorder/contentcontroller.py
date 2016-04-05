import os
import re
import requests
import json

from six.moves.urllib.parse import quote

from bottle import Bottle, request, redirect, HTTPError, response

from pywb.utils.timeutils import timestamp_now

from urlrewrite.rewriterapp import RewriterApp

from webrecorder.basecontroller import BaseController

from webagg.utils import StreamIter
from io import BytesIO


# ============================================================================
class ContentController(BaseController, RewriterApp):
    DEF_REC_NAME = 'my-recording'

    PATHS = {'live': '{replay_host}/live/resource/postreq?url={url}&closest={closest}',
             'record': '{record_host}/record/live/resource/postreq?url={url}&closest={closest}&param.recorder.user={user}&param.recorder.coll={coll}&param.recorder.rec={rec}',
             'replay': '{replay_host}/replay/resource/postreq?url={url}&closest={closest}&param.replay.user={user}&param.replay.coll={coll}&param.replay.rec={rec}',
             'replay-coll': '{replay_host}/replay-coll/resource/postreq?url={url}&closest={closest}&param.user={user}&param.coll={coll}',

             'download': '{record_host}/download?user={user}&coll={coll}&rec={rec}&filename={filename}&type={type}',
             'download_filename': '{title}-{timestamp}.warc.gz'
            }

    WB_URL_RX = re.compile('((\d*)([a-z]+_)?/)?(https?:)?//.*')

    def __init__(self, app, jinja_env, manager, config):
        self.record_host = os.environ['RECORD_HOST']
        self.replay_host = os.environ['WEBAGG_HOST']

        BaseController.__init__(self, app, jinja_env, manager, config)
        RewriterApp.__init__(self, framed_replay=True, jinja_env=jinja_env)

    def init_routes(self):
        # REDIRECTS
        @self.app.route(['/record/<wb_url:path>', '/anonymous/record/<wb_url:path>'], method='ANY')
        def redir_anon_rec(wb_url):
            wb_url = self.add_query(wb_url)
            new_url = '/anonymous/{rec}/record/{url}'.format(rec=self.DEF_REC_NAME,
                                                             url=wb_url)
            return redirect(new_url)

        @self.app.route('/replay/<wb_url:path>', method='ANY')
        def redir_anon_replay(wb_url):
            wb_url = self.add_query(wb_url)
            new_url = '/anonymous/{url}'.format(url=wb_url)
            return redirect(new_url)

        # LIVE DEBUG
        @self.app.route('/live/<wb_url:path>', method='ANY')
        def live(wb_url):
            request.path_shift(1)

            return self.handle_anon_content(wb_url, rec='', type='live')

        # ANON ROUTES
        @self.app.route('/anonymous/<rec_name>/record/<wb_url:path>', method='ANY')
        def anon_record(rec_name, wb_url):
            request.path_shift(3)

            return self.handle_anon_content(wb_url, rec=rec_name, type='record')

        @self.app.route('/anonymous/<wb_url:path>', method='ANY')
        def anon_replay(wb_url):
            rec_name = '*'

            # recording replay
            if not self.WB_URL_RX.match(wb_url) and '/' in wb_url:
                rec_name, wb_url = wb_url.split('/', 1)

                # todo: edge case: something like /anonymous/example.com/
                # should check if 'example.com' is a recording, otherwise assume url?
                #if not wb_url:
                #    wb_url = rec_name
                #    rec_name = '*'

            if rec_name == '*':
                request.path_shift(1)
                type_ = 'replay-coll'

            else:
                request.path_shift(2)
                type_ = 'replay'

            return self.handle_anon_content(wb_url, rec=rec_name, type=type_)

        @self.app.get('/anonymous/<rec>/download')
        def anon_download_rec_warc(rec):
            user = self.get_anon_user()
            coll = 'anonymous'

            recinfo = self.manager.get_recording(user, coll, rec)
            if not recinfo:
                self._raise_error(404, 'Recording not found',
                                  id=rec)

            title = recinfo.get('title', rec)
            return self.handle_download('rec', user, coll, rec, title)

        @self.app.get('/anonymous/download')
        def anon_download_coll_warc():
            user = self.get_anon_user()
            coll = 'anonymous'

            collinfo = {}
            collinfo = self.manager.get_collection(user, coll)
            if not collinfo:
                self._raise_error(404, 'Collection not found',
                                  id=coll)

            title = collinfo.get('title', coll)
            return self.handle_download('coll', user, coll, '*', title)

    def handle_download(self, type, user, coll, rec, title):
        now = timestamp_now()
        filename = self.PATHS['download_filename'].format(title=title,
                                                          timestamp=now)

        download_url = self.PATHS['download']
        download_url = download_url.format(record_host=self.record_host,
                                           user=user,
                                           coll=coll,
                                           rec=rec,
                                           type=type,
                                           filename=filename)

        res = requests.get(download_url, stream=True)

        if res.status_code >= 400:  #pragma: no cover
            try:
                res.raw.close()
            except:
                pass

            self._raise_error(400, 'Unable to download WARC')

        response.headers['Content-Type'] = 'application/octet-stream'
        response.headers['Content-Disposition'] = 'attachment; filename=' + quote(filename)

        length = res.headers.get('Content-Length')
        if length:
            response.headers['Content-Length'] = length

        encoding = res.headers.get('Transfer-Encoding')
        if encoding:
            response.headers['Transfer-Encoding'] = encoding

        return StreamIter(res.raw)

    def get_anon_user(self):
        sesh = request.environ['webrec.session']
        user = sesh.anon_user.replace('@anon-', 'anon/')
        return user

    def handle_anon_content(self, wb_url, rec, type):
        wb_url = self.add_query(wb_url)
        sesh = request.environ['webrec.session']
        user = self.get_anon_user()
        coll = 'anonymous'

        if type == 'record' or type == 'replay':
            if type == 'record' and not sesh.is_anon():
                sesh.set_anon()

            if not self.manager.has_recording(user, coll, rec):
                title = rec
                rec = self.sanitize_title(title)

                if not self.manager.has_collection(user, coll):
                    self.manager.create_collection(user, coll)

                if type == 'record':
                    if rec == title or not self.manager.has_recording(user, coll, rec):
                        result = self.manager.create_recording(user, coll, rec, title)

                if rec != title:
                    target = self.get_host()
                    target += request.script_name.replace(title, rec)
                    target += wb_url
                    redirect(target)

                if type == 'replay':
                    raise HTTPError(404, 'No Such Recording')

        return self.render_content(wb_url, user=user,
                                           coll=coll,
                                           rec=rec,
                                           type=type)

    def add_query(self, url):
        if request.query_string:
            url += '?' + request.query_string

        return url

    def get_top_frame_params(self, wb_url, kwargs):
        type = kwargs['type']
        if type == 'live':
            return

        request.environ['webrec.template_params']['curr_mode'] = type
        info = self.manager.get_content_inject_info(kwargs['user'],
                                                    kwargs['coll'],
                                                    kwargs['rec'])
        info = json.dumps(info)
        #request.environ['webrec.template_params']['info'] = info
        return {'info': info}

    def get_upstream_url(self, url, wb_url, closest, kwargs):
        type = kwargs['type']
        upstream_url = self.PATHS[type].format(url=quote(url),
                                               closest=closest,
                                               record_host=self.record_host,
                                               replay_host=self.replay_host,
                                               **kwargs)

        return upstream_url

    def _add_custom_params(self, cdx, kwargs):
        type = kwargs['type']
        if type in ('live', 'record'):
            cdx['is_live'] = 'true'
