from __future__ import unicode_literals
import os, sys, re, errno
import ssl

if sys.version_info[0] >= 3:
    from urllib.request import Request
else:
    from urllib2 import Request

try:
    from json import loads as parse_json, dumps as compile_json
except ImportError:
    from simplejson import loads as parse_json, dumps as compile_json
from email.parser import Parser
from txclib.packages import urllib3
from txclib.packages.urllib3.packages import six
from txclib.packages.urllib3.packages.six.moves import input
from txclib.urls import API_URLS
from txclib.exceptions import UnknownCommandError
from txclib.paths import posix_path, native_path, posix_sep
from txclib.web import user_agent_identifier, certs_file
from txclib.log import logger
from txclib.packages.urllib3.exceptions import SSLError


# Initialize http and https pool managers
num_pools = 1
managers = {}
if "http_proxy" in os.environ:
    proxy_url = os.environ["http_proxy"]
    managers["http"] = urllib3.ProxyManager(
        proxy_url=proxy_url,
        num_pools=num_pools
    )
else:
    managers["http"] = urllib3.PoolManager(num_pools=num_pools)

if "https_proxy" in os.environ:
    proxy_url = os.environ["https_proxy"]
    managers["https"] = urllib3.ProxyManager(
        proxy_url=proxy_url,
        num_pools=num_pools,
        cert_reqs='CERT_NONE',
        assert_hostname=False
    )
else:
    managers["https"] = urllib3.PoolManager(
        num_pools=num_pools,
        cert_reqs='CERT_NONE',
        assert_hostname=False
    )


class HttpNotFound(Exception):
    pass


def find_dot_tx(path=os.path.curdir, previous=None):
    """Return the path where .tx folder is found.

    The 'path' should be a DIRECTORY.
    This process is functioning recursively from the current directory to each
    one of the ancestors dirs.
    """
    path = os.path.abspath(path)
    if path == previous:
        return None
    joined = os.path.join(path, ".tx")
    if os.path.isdir(joined):
        return path
    else:
        return find_dot_tx(os.path.dirname(path), path)


#################################################
# Parse file filter expressions and create regex

def regex_from_filefilter(file_filter, root_path = os.path.curdir):
    """Create proper regex from <lang> expression."""
    # Force expr to be a valid regex expr (escaped) but keep <lang> intact
    expr_re = re.escape(
        posix_path(os.path.join(root_path, native_path(file_filter)))
    )
    expr_re = expr_re.replace("\\<lang\\>", '<lang>').replace(
        '<lang>', '([^%(sep)s]+)' % { 'sep': re.escape(posix_sep)})

    return "^%s$" % expr_re


TX_URLS = {
    'resource': '(?P<hostname>https?://(\w|\.|:|-)+)/projects/p/(?P<project>(\w|-)+)/resource/(?P<resource>(\w|-)+)/?$',
    'project': '(?P<hostname>https?://(\w|\.|:|-)+)/projects/p/(?P<project>(\w|-)+)/?$',
}


def parse_tx_url(url):
    """
    Try to match given url to any of the valid url patterns specified in
    TX_URLS. If not match is found, we raise exception
    """
    for type_ in list(TX_URLS.keys()):
        pattern = TX_URLS[type_]
        m = re.match(pattern, url)
        if m:
            return type_, m.groupdict()
    raise Exception(
        "tx: Malformed url given. Please refer to our docs: http://bit.ly/txautor"
    )


def determine_charset(response):
    content_type = response.headers.get('content-type', None)
    if content_type:
        message = Parser().parsestr("Content-type: %s" % content_type)
        for charset in message.get_charsets():
            if charset:
                return charset
    return "utf-8"


def make_request_with_connection_info(
    method, host, url, connection_info, fields=None
):
    basic_auth = "{0}:{1}".format(
        connection_info["username"],
        connection_info["password"]
    )
    headers = urllib3.util.make_headers(
        basic_auth=basic_auth,
        accept_encoding=True,
        user_agent=user_agent_identifier(),
        keep_alive=True
    )
    request = Request(host + url, None, headers)
    request.type = method
    response = None
    try:
        if host.startswith("http://"):
            scheme = "http"
        elif host.startswith("https://"):
            scheme = "https"
        else:
            raise Exception("Unknown scheme")
        manager = managers[scheme]
        response = manager.request(
            request.get_type(),
            request.get_full_url(),
            headers=dict(request.header_items()),
            fields=fields
        )
        data = response.data
        charset = determine_charset(response)
        if isinstance(data, bytes):
            data = data.decode("utf-8")
        if response.status < 200 or response.status >= 400:
            if response.status == 404:
                raise HttpNotFound(data)
            else:
                raise Exception(data)
        return data, charset
    except SSLError:
        logger.error("Invalid SSL certificate")
        raise
    finally:
        if response is not None:
            response.close()


def create_connection_info(username, password):
    return {
        "username": username,
        "password": password
    }

    
def make_request(method, host, url, username, password, fields=None):
    info = create_connection_info(username, password)
    return make_request_with_connection_info(method, host, url, info, fields)


def get_details(api_call, username, password, *args, **kwargs):
    """
    Get the tx project info through the API.

    This function can also be used to check the existence of a project.
    """
    url = API_URLS[api_call] % kwargs
    try:
        data, charset = make_request('GET', kwargs['hostname'], url, username, password)
        return parse_json(data)
    except Exception as e:
        logger.debug(six.u(str(e)))
        raise


def valid_slug(slug):
    """
    Check if a slug contains only valid characters.

    Valid chars include [-_\w]
    """
    try:
        a, b = slug.split('.')
    except ValueError:
        return False
    else:
        if re.match("^[A-Za-z0-9_-]*$", a) and re.match("^[A-Za-z0-9_-]*$", b):
            return True
        return False


def discover_commands():
    """
    Inspect commands.py and find all available commands
    """
    import inspect
    from txclib import commands

    command_table = {}
    fns = inspect.getmembers(commands, inspect.isfunction)

    for name, fn in fns:
        if name.startswith("cmd_"):
            command_table.update({
                name.split("cmd_")[1]:fn
            })

    return command_table


def exec_command(command, *args, **kwargs):
    """
    Execute given command
    """
    commands = discover_commands()
    try:
        cmd_fn = commands[command]
    except KeyError:
        raise UnknownCommandError
    cmd_fn(*args,**kwargs)


def mkdir_p(path):
    try:
        if path:
            os.makedirs(path)
    except OSError as exc:
        if exc.errno == errno.EEXIST:
            pass
        else:
            raise


def confirm(prompt='Continue?', default=True):
    """
    Prompt the user for a Yes/No answer.

    Args:
        prompt: The text displayed to the user ([Y/n] will be appended)
        default: If the default value will be yes or no
    """
    valid_yes = ['Y', 'y', 'Yes', 'yes', ]
    valid_no = ['N', 'n', 'No', 'no', ]
    if default:
        prompt = prompt + '[Y/n]'
        valid_yes.append('')
    else:
        prompt = prompt + '[y/N]'
        valid_no.append('')

    ans = input(prompt)
    while (ans not in valid_yes and ans not in valid_no):
        ans = input(prompt)

    return ans in valid_yes


# Stuff for command line colored output

COLORS = [
    'BLACK', 'RED', 'GREEN', 'YELLOW',
    'BLUE', 'MAGENTA', 'CYAN', 'WHITE'
]

DISABLE_COLORS = False


def color_text(text, color_name, bold=False):
    """
    This command can be used to colorify command line output. If the shell
    doesn't support this or the --disable-colors options has been set, it just
    returns the plain text.

    Usage:
        print "%s" % color_text("This text is red", "RED")
    """
    if color_name in COLORS and not DISABLE_COLORS:
        return '\033[%s;%sm%s\033[0m' % (
            int(bold), COLORS.index(color_name) + 30, text)
    else:
        return text


def files_in_project(curpath):
    """
    Iterate over the files in the project.

    Return each file under ``curpath`` with its absolute name.
    """
    for root, dirs, files in os.walk(curpath, followlinks=True):
        for f in files:
            yield os.path.abspath(os.path.join(root, f))
