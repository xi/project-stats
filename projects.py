from xml.etree import ElementTree
import argparse
import json
import logging
import os
import re
import subprocess

from filecachetools import ttl_cache
import requests
import yaml
from dateutil import parser as dt

try:
    from cheesecake.cheesecake_index import Cheesecake
except ImportError:
    Cheesecake = None

SOURCES = ['github', 'gitorious', 'local', 'pypi', 'bower', 'travis']

KEYS = [
    'name',
    'description',
    'version',
    'homepage',
    'created',
    'updated',
    'license',
    'language',
    'tests',
    'commit_count',
    'file_count',
    'unstaged_files',
    'contributors',
    'downloads',
    'open_issues',
    'open_pull_requests',
    'forks_count',
    'stargazers_count',
    'subscribers_count',
    'watchers_count',
    'cheesecake_index',
]


def r_get(d, *keys):
    """Recursively get key from dict or return None."""
    if len(keys) == 0:
        return d
    elif keys[0] in d:
        return r_get(d[keys[0]], *keys[1:])


class Claims(object):
    def __init__(self):
        self._list = []

    def _index(self, key):
        i = 0
        for k, value in self._list:
            if key == k:
                return i
            i += 1
        raise KeyError

    def add(self, value, source):
        if not value and value != 0:
            return
        try:
            i = self._index(value)
        except KeyError:
            self._list.append((value, []))
            i = -1
        self._list[i][1].append(source)

    def format(self, show_sources=True):
        def _format_claim(value, sources):
            s = str(value)
            if show_sources:
                s += ' (%s)' % ', '.join(sources)
            return s
        return '; '.join([_format_claim(v, srcs) for v, srcs in self._list])


class ClaimsDict(object):
    def __init__(self, keys, short=9):
        self._keys = keys
        self._short = short
        self._data = {}

    def update(self, data, source):
        for key in data:
            if key not in self._keys:
                raise KeyError(key)
        if source not in self._data:
            self._data[source] = {}
        self._data[source].update(data)

    def __getitem__(self, key):
        if key not in self._keys:
            raise KeyError(key)
        claims = Claims()
        for source, data in self._data.items():
            if key in data:
                claims.add(data[key], source)
        return claims

    def get(self, key, default=None):
        try:
            return self.__getitem__(key)
        except KeyError:
            return default

    def format(self, short=False, indent=0, show_sources=True):
        keys = self._keys[:self._short] if short else self._keys
        lines = []
        for key in keys:
            value = self.get(key)
            formated_value = value.format(show_sources=show_sources)
            if not formated_value:
                continue
            lines.append(' ' * indent + '%s: %s' % (key, formated_value))
        return '\n'.join(lines)


@ttl_cache('xi-projects-cheesecake', ttl=3600)
def cheesecake_index(name):
    if Cheesecake is not None:
        # does not seem to be meant to be used as a library
        c = Cheesecake(name=name, quiet=True, lite=True)
        value = c.index.compute_with(c)
        max_value = c.index.max_value
        c.cleanup()
        return value * 100 / max_value
    else:
        return None


@ttl_cache('xi-projects-bower', ttl=3600)
def get_bower_info(name):
    try:
        s = subprocess.check_output(['bower', 'info', name])
    except OSError:
        return None

    # re handles \n specially, so it is replaced by \t
    s = '\t'.join(s.splitlines())

    # strip uninteresting information
    s = re.sub('.*\t{', '{', s)
    s = re.sub('\t}.*', '\t}', s)

    # this is Javascript object syntax, not strict JSON
    s = re.sub('\t( *)([a-z]*): ', '\t\\1"\\2": ', s)
    s = s.replace('\'', '"')

    return json.loads(s)


@ttl_cache('xi-projects', ttl=3600)
def get_json(url, user=None, password=None):
    assert not ((user is None) ^ (password is None))

    if user is None:
        req = requests.get(url)
    else:
        req = requests.get(
            url, auth=requests.auth.HTTPBasicAuth(user, password))

    req.raise_for_status()
    return req.json()


@ttl_cache('xi-projects', ttl=3600)
def get_xml(url):
    req = requests.get(url)
    return ElementTree.fromstring(req.content)


def get_github(url, user=None, password=None):
    def _get_json(url):
        data = get_json(url, user=user, password=password)
        if 'documentation_url' in data:
            raise requests.RequestError(data['documentation_url'])
        return data

    def get_all_pages(url):
        l = []
        new = True
        page = 1
        while new:
            u = url + '?page=%i' % page
            new = _get_json(u)
            l += new
            page += 1
        return l

    api_url = re.sub(
        'https?://github.com', 'https://api.github.com/repos', url)
    data = _get_json(api_url)

    def get_latest_tag():
        tags = get_all_pages(data['tags_url'])
        tags = map(lambda tag: tag['name'], tags)
        if len(tags) > 0:
            return max(tags, key=lambda tag: tag.lstrip('v'))

    def get_open_pull_requests():
        url = data['pulls_url'].replace('{/number}', '')
        pulls = _get_json(url)
        return len(pulls)

    return {
        'name': data['name'],
        'description': data['description'],
        'created': dt.parse(data['created_at']),
        'updated': dt.parse(data['updated_at']),
        'homepage': data['homepage'],
        'language': data['language'],
        'watchers_count': data['watchers_count'],
        'stargazers_count': data['stargazers_count'],
        'subscribers_count': data['subscribers_count'],
        'forks_count': data['forks_count'],
        'open_issues': data['open_issues'],
        'open_pull_requests': get_open_pull_requests(),
        'version': get_latest_tag(),
    }


def get_gitorious(url):
    api_url = url.rstrip('/') + '.xml'
    data = get_xml(api_url)

    return {
        'description': data.find('description').text,
        'name': data.find('name').text,
        'created': dt.parse(data.find('created-at').text),
        'updated': dt.parse(data.find('last-pushed-at').text),
    }


def get_local(path):
    def git(cmd, *args):
        return subprocess.check_output(['git', '-C', path, cmd] + list(args))

    def get_latest_tag():
        tags = git('tag').splitlines()
        if len(tags) > 0:
            return max(tags, key=lambda tag: tag.lstrip('v'))

    def get_rev_datetime(rev):
        return git('show', '-s', '--format=%ai', rev).rstrip()

    tail = git('rev-list', 'HEAD').splitlines()[-1]

    return {
        'name': os.path.basename(path.rstrip('/')),
        'file_count': len(git('ls-files').splitlines()),
        'unstaged_files': 'Changes not staged for commit' in git('status'),
        'commit_count': len(git('rev-list', 'HEAD').splitlines()),
        'version': get_latest_tag(),
        'contributors': len(git('shortlog', '-s').splitlines()),
        'created': dt.parse(get_rev_datetime(tail)),
        'updated': dt.parse(get_rev_datetime('HEAD')),
    }


def get_pypi(url):
    data = get_json(url + '/json')

    return {
        'version': data['info']['version'],
        'description': data['info']['summary'],
        'downloads': data['info']['downloads'],
        'name': data['info']['name'],
        'license': data['info']['license'],
        'homepage': data['info']['home_page'],
        'cheesecake_index': cheesecake_index(data['info']['name']),
    }


def get_bower(name):
    data = get_bower_info(name)
    if data is None:
        return {}
    else:
        return {
            'name': data['name'],
            'version': data.get('version'),
            'homepage': data.get('homepage'),
            'description': data.get('description'),
            'license': data.get('license'),
        }


def get_travis(url):
    api_url = re.sub(
        'https?://travis-ci.org', 'https://api.travis-ci.org/repos', url)
    data = get_json(api_url)
    return {
        'description': data['description'],
        'tests': data['last_build_result'] == 0,
    }


def load_config(path):
    with open(path) as fh:
        return yaml.load(fh)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('query', nargs='?', help='optionally filter projects')
    parser.add_argument(
        '-l', '--list',
        action='store_true',
        help='only list projects; do not show any stats')
    parser.add_argument(
        '-s', '--short',
        action='store_true',
        help='show only basic stats')
    parser.add_argument(
        '-c', '--config',
        default='projects.yml')
    parser.add_argument(
        '-S', '--show-sources',
        action='store_true',
        help='show a source for each claim')

    return parser.parse_args()


def main():
    args = parse_args()
    config = load_config(os.path.expanduser(args.config))

    keys = config['projects'].keys()
    if args.query is not None:
        keys = filter(lambda k: args.query.lower() in k.lower(), keys)

    if args.list:
        for key in keys:
            print key
        return

    for key in keys:
        try:
            project = config['projects'][key]
            claims = ClaimsDict(KEYS)
            if not args.list:
                for source in SOURCES:
                    if source in project:
                        fn = globals()['get_' + source]
                        if source == 'github':
                            data = fn(
                                project[source],
                                user=r_get(config, 'github', 'user'),
                                password=r_get(config, 'github', 'password'))
                        else:
                            data = fn(project[source])
                        claims.update(data, source)
                print('%s\n%s\n' % (key, claims.format(
                    indent=2,
                    short=args.short,
                    show_sources=args.show_sources)))
        except Exception as e:
            logging.error("Error while gathering stats for %s: %s" % (key, e))


if __name__ == '__main__':
    main()
