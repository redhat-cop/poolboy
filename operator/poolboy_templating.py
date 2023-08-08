import copy
import functools
import jinja2
import jmespath
import json
import pytimeparse
import random
import re

from datetime import datetime, timedelta, timezone
from distutils.util import strtobool
from strgen import StringGenerator

class TimeStamp(object):
    def __init__(self, set_datetime=None):
        if not set_datetime:
            self.datetime = datetime.utcnow()
        elif isinstance(set_datetime, datetime):
            self.datetime = set_datetime
        elif isinstance(set_datetime, str):
            self.datetime = datetime.strptime(set_datetime, "%Y-%m-%dT%H:%M:%SZ")
        else:
            self.datetime = set_datetime

    def __call__(self, arg):
        return TimeStamp(arg)

    def __eq__(self, other):
        return self.datetime == other.datetime

    def __ne__(self, other):
        return self.datetime != other.datetime

    def __ge__(self, other):
        return self.datetime >= other.datetime

    def __gt__(self, other):
        return self.datetime > other.datetime

    def __le__(self, other):
        return self.datetime <= other.datetime

    def __lt__(self, other):
        return self.datetime < other.datetime

    def __str__(self):
        return self.datetime.strftime('%FT%TZ')

    def __add__(self, interval):
        return self.add(interval)

    def add(self, interval):
        ret = TimeStamp(self.datetime)
        if isinstance(interval, timedelta):
            ret.datetime += interval
        elif isinstance(interval, str):
            ret.datetime += timedelta(seconds=pytimeparse.parse(interval))
        elif isinstance(interval, number.Number):
            ret.datetime += timedelta(seconds=interval)
        else:
            raise Exception(f"Unable to add {interval} to Timestamp")
        return ret

    @property
    def utcnow(self):
        return TimeStamp()

def error_if_undefined(result):
    if isinstance(result, jinja2.Undefined):
        result._fail_with_undefined_error()
    else:
        return result

def seconds_to_interval(seconds:int) -> str:
    if seconds % 86400 == 0:
        return f"{int(seconds / 86400)}d"
    elif seconds % 3600 == 0:
        return f"{int(seconds / 3600)}h"
    elif seconds % 60 == 0:
        return f"{int(seconds / 60)}m"
    else:
        return f"{int(seconds)}s"

jinja2envs = {
    'jinja2': jinja2.Environment(
        finalize = error_if_undefined,
        undefined = jinja2.ChainableUndefined,
    ),
}
jinja2envs['jinja2'].filters['bool'] = lambda x: bool(strtobool(x)) if isinstance(x, str) else bool(x)
jinja2envs['jinja2'].filters['json_query'] = lambda x, query: jmespath.search(query, x)
jinja2envs['jinja2'].filters['merge_list_of_dicts'] = lambda a: functools.reduce(lambda d1, d2: {**(d1 or {}), **(d2 or {})}, a)
jinja2envs['jinja2'].filters['object'] = lambda x: json.dumps(x)
jinja2envs['jinja2'].filters['parse_time_interval'] = lambda x: timedelta(seconds=pytimeparse.parse(x))
jinja2envs['jinja2'].filters['strgen'] = lambda x: StringGenerator(x).render()
jinja2envs['jinja2'].filters['to_datetime'] = lambda s, f='%Y-%m-%d %H:%M:%S': datetime.strptime(s, f)
jinja2envs['jinja2'].filters['to_json'] = lambda x: json.dumps(x)

# Regex to detect if it looks like this value should be rendered as a raw type
# rather than a string.
#
# So given a template in YAML of:
#
# example:
#   boolean_as_string: "{{ 1 == 1 }}"
#   boolean_raw: "{{ (1 == 1) | bool }}"
#   float_as_string: "{{ 1 / 3 }}"
#   float_raw: "{{ (1 / 3) | float }}"
#   number_as_string: "{{ 1 + 1 }}"
#   number_raw: "{{ (1 + 1) | int }}"
#   object_as_string: "{{ {'user': {'name': 'alice'}} }}"
#   object_raw: "{{ {'user': {'name': 'alice'}} | object }}"
#
# Will render to:
#
# example:
#   boolean_as_string: 'True'
#   boolean_raw: true
#   float_as_string: '0.3333333333333333'
#   float_raw: 0.3333333333333333
#   number_as_string: '2'
#   number_raw: 2
#   object_as_string: '{''user'': {''name'': ''alice''}}'
#   object_raw:
#     user:
#       name: alice
type_filter_match_re = re.compile(r'^{{(?!.*{{).*\| *(bool|float|int|object) *}}$')

def j2now(utc=False, fmt=None):
    dt = datetime.now(timezone.utc if utc else None)
    return dt.strftime(fmt) if fmt else dt

def jinja2process(template, omit=None, template_style='jinja2', variables={}):
    variables = copy.copy(variables)
    variables['datetime'] = datetime
    variables['now'] = j2now
    variables['omit'] = omit
    variables['timedelta'] = timedelta
    variables['timezone'] = timezone
    variables['timestamp'] = TimeStamp()
    jinja2env = jinja2envs.get(template_style)
    j2template = jinja2env.from_string(template)
    template_out = j2template.render(variables)

    type_filter_match = type_filter_match_re.match(template)
    if type_filter_match:
        type_filter = type_filter_match.group(1)
        try:
            if type_filter == 'bool':
                return bool(strtobool(template_out))
            elif type_filter == 'float':
                return float(template_out)
            elif type_filter == 'int':
                return int(template_out)
            elif type_filter == 'object':
                return json.loads(template_out)
        except ValueError:
            pass
    else:
        return template_out

def recursive_process_template_strings(template, template_style='jinja2', variables={}):
    omit = '__omit_place_holder__' + ''.join(random.choices('abcdef0123456789', k=40))
    return __recursive_strip_omit(
        __recursive_process_template_strings(
            omit = omit,
            template = template,
            template_style = template_style,
            variables = variables,
        ),
        omit = omit,
    )

def __recursive_process_template_strings(template, omit, template_style, variables):
    if isinstance(template, dict):
        return {
            key: __recursive_process_template_strings(val, omit=omit, template_style=template_style, variables=variables)
            for key, val in template.items()
        }
    elif isinstance(template, list):
        return [
            __recursive_process_template_strings(item, omit=omit, template_style=template_style, variables=variables)
            for item in template
        ]
    elif isinstance(template, str):
        return jinja2process(template, omit=omit, template_style=template_style, variables=variables)
    else:
        return template

def __recursive_strip_omit(value, omit):
    if isinstance(value, dict):
        return {
            key: __recursive_strip_omit(val, omit=omit)
            for key, val in value.items()
            if val != omit
        }
    elif isinstance(value, list):
        return [
            __recursive_strip_omit(item, omit=omit) for item in value if item != omit
        ]
    elif value != omit:
        return value
