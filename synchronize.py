# coding: utf-8

import os
import re
import xmlrpclib
import requests
import subprocess
import ConfigParser
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)
ch = logging.StreamHandler()
ch.setLevel(logging.INFO)
ch.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger.addHandler(ch)


class Rpc(object):

    def __init__(self, configuration):
        self.url = '%s/xmlrpc/' % configuration.get('odoo', 'url')
        self.db = configuration.get('odoo', 'db')
        self.username = configuration.get('odoo', 'username')
        self.password = configuration.get('odoo', 'password')

    def login(self):
        self._user = xmlrpclib.ServerProxy(self.url + 'common').login(
            self.db, self.username, self.password)
        if not self._user:
            raise Exception('Not login into %s' % self.url)
        logger.info('Rpc.login > Log in on odoo %s (%s@%s)', self.url,
                    self.username, self.db)

    def execute(self, *args, **kargs):
        logger.info('Rpc.execute >  Url : %s, params : %s',
                    self.url + 'object', args)
        return xmlrpclib.ServerProxy(self.url + 'object').execute(
            self.db, self._user, self.password, *args, **kargs)


class WeblateAPI(object):

    def __init__(self, configuration):
        self._weblate_container = False
        if configuration.has_section('docker'):
            self._weblate_container = configuration.get('docker', 'name')

    def _init_api(self, url, token):
        self._url = url
        self._token = token
        self._session = requests.Session()
        self._session.headers.update({
            'Accept': 'application/json',
            'User-Agent': 'syn_runbot_weblate',
            'Authorization': 'Token %s' % self._token
        })
        self._load_projects()
        logger.info('WeblateAPI._init_api > Found %s projects',
                    len(self._api_projects))

    def _load_projects(self, page=1):
        if page == 1:
            self._api_projects = []
        response = self._session.get('%s/projects/?page=%s' %
                                     (self._url, page))
        response.raise_for_status()
        data = response.json()
        self._api_projects.extend(data['results'])
        if data['next']:
            self._load_projects(data['next'].split('=')[-1])

    def create_project(self, repo, name):
        slug = name
        slug = slug.replace('/', '_').replace(':', '_').replace('.', '_')
        slug = slug.replace(' ', '').replace('(', '_').replace(')', '_')
        if (not any([pre for pre in ['http://', 'https://'] if pre in repo])
                and '@' in repo):
            repo = 'http://' + repo.split('@')[1:].pop().replace(':', '/')
        cmd = []
        if self._weblate_container:
            cmd.extend(['docker', 'exec', self._weblate_container])
        cmd.extend(['django-admin', 'shell', '-c',
                    'import weblate.trans.models.project as project;'
                    'project.Project(name=\'{0}\', slug=\'{1}\', web=\'{2}\')'
                    '.save()'.format(name, slug, repo)])
        logger.info('WeblateAPI.create_project > Create project %s '
                    '(slug=%s, repo=%s, cmd=%s)', name, slug, repo, cmd)
        try:
            print subprocess.check_output(cmd)
        except subprocess.CalledProcessError:
            logger.error('WeblateAPI.create_project > Error processing the '
                         'project %s', name)
            return False
        self._load_projects()
        response = self._session.get(self._url + '/projects/%s/' % slug)
        response.raise_for_status()
        return response.json()

    def find_or_create_project(self, project):
        slug = project['repo']
        slug = slug.replace(':', '/')
        slug = re.sub('.+@', '', slug)
        slug = re.sub('.git$', '', slug)
        slug = re.sub('^https://', '', slug)
        slug = re.sub('^http://', '', slug)
        match = re.search(
            r'(?P<host>[^/]+)/(?P<owner>[^/]+)/(?P<repo>[^/]+)', slug)
        if match:
            slug = ("%(host)s:%(owner)s/%(repo)s (%(branch)s)" %
                    dict(match.groupdict(), branch=project['branch']))
        for pro in self._api_projects:
            if slug == pro['name']:
                logger.info('WeblateAPI.find_or_create_project > Found '
                            'project %s', pro['name'])
                return pro
        return self.create_project(project['repo'], slug)

    def create_component(self, project, branch):
        cmd = []
        if self._weblate_container:
            cmd.extend(['docker', 'exec', self._weblate_container])
        repo = project['web']
        repo = re.sub('^https://', '', repo)
        repo = re.sub('^http://', '', repo)
        match = re.search(
            r'(?P<host>[^/]+)/(?P<owner>[^/]+)/(?P<repo>[^/]+)', repo)
        if match:
            repo = ("git@%(host)s:%(owner)s/%(repo)s" %
                    dict(match.groupdict()))
        cmd.extend(['django-admin',
                    'import_project', project['slug'], repo,
                    branch['branch_name'], '**/i18n/*.po'])
        logger.info('WeblateAPI.create_component > Create component %s '
                    '(cmd=%s)', project['slug'], cmd)
        try:
            print subprocess.check_output(cmd)
        except subprocess.CalledProcessError:
            logger.error('WeblateAPI.create_component > Error processing the '
                         'project %s on branch %s', project['slug'],
                         branch['branch_name'])
            return False

    def import_from_runbot(self, repo, branches):
        if not branches:
            return
        self._init_api(repo['weblate_url'], repo['weblate_token'])
        for branch in branches:
            project = self.find_or_create_project({
                'repo': repo['name'],
                'branch': branch['branch_name']
            })
            if project:
                self.create_component(project, branch)

    def _request_api(self, url):
        response = self._session.get(self._url + url)
        response.raise_for_status()
        return response.json()


class SynRunbotWeblate(object):

    def __init__(self, configuration):
        self._rpc = Rpc(configuration)
        self._wlapi = WeblateAPI(configuration)

    def sync(self):
        self._rpc.login()
        ids = self._rpc.execute(
            'runbot.repo', 'search', [['weblate_token', '!=', ''],
                                      ['weblate_url', '!=', '']])
        repos = self._rpc.execute('runbot.repo', 'read', ids)
        for repo in repos:
            ids = self._rpc.execute(
                'runbot.branch', 'search', [['uses_weblate', '=', True],
                                            ['repo_id', '=', repo['id']]])
            branches = self._rpc.execute('runbot.branch', 'read', ids)
            if not branches:
                logger.warning('SynRunbotWeblate.sync > Repo no found '
                               'branches (id=%s, name=%s)', repo['id'],
                               repo['name'])
            self._wlapi.import_from_runbot(repo, branches)
        cmd = []
        if configuration.has_section('docker'):
            weblate_container = configuration.get('docker', 'name')
        if weblate_container:
            cmd.extend(['docker', 'exec', weblate_container])
        cmd.extend(['find', '/app/data/vcs',
                    '-type', 'd', '-name', "tmp*", '-exec',
                    'rm', '-rf', "{}", '+'])
        print cmd
        try:
            print subprocess.check_output(cmd)
        except subprocess.CalledProcessError:
            logger.error('SynRunbotWeblate.sync > Error cleaning the temporal '
                         'folder /app/data/vcs')
        return 0


if __name__ == '__main__':
    configuration = ConfigParser.ConfigParser()
    configuration.readfp(
        open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
             'synchronize.cfg')))
    exit(SynRunbotWeblate(configuration).sync())
