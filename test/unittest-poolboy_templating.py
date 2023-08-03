#!/usr/bin/env python3

import unittest
import sys
sys.path.append('../operator')

from poolboy_templating import recursive_process_template_strings, seconds_to_interval

class TestJsonPatch(unittest.TestCase):
    def test_00(self):
        template = {}
        template_vars = {}
        self.assertEqual(
            recursive_process_template_strings(template, 'jinja2', template_vars),
            {}
        )

    def test_01(self):
        template = {
            "a": "b",
            "b": "2",
            "c": 3,
            "d": {
                "e": ["a", "b", "c"]
            }
        }
        template_vars = {}
        self.assertEqual(
            recursive_process_template_strings(template, 'jinja2', template_vars),
            template
        )

    def test_02(self):
        template = {
            "a": "{{ foo }}",
        }
        template_vars = {
            "foo": "bar"
        }
        self.assertEqual(
            recursive_process_template_strings(template, 'jinja2', template_vars),
            {
                "a": "bar"
            }
        )

    def test_03(self):
        template = {
            "a": ["{{ foo }}"],
        }
        template_vars = {
            "foo": "bar"
        }
        self.assertEqual(
            recursive_process_template_strings(template, 'jinja2', template_vars),
            {
                "a": ["bar"]
            }
        )

    def test_04(self):
        template = {
            "a": "{{ foo }}"
        }
        template_vars = {
            "foo": True
        }
        self.assertEqual(
            recursive_process_template_strings(template, 'jinja2', template_vars),
            {
                "a": "True"
            }
        )

    def test_05(self):
        template = {
            "a": "{{ foo | bool }}"
        }
        template_vars = {
            "foo": True
        }
        self.assertEqual(
            recursive_process_template_strings(template, 'jinja2', template_vars),
            {
                "a": True
            }
        )

    def test_06(self):
        template = {
            "a": "{{ numerator / denominator }}"
        }
        template_vars = {
            "numerator": 1,
            "denominator": 2,
        }
        self.assertEqual(
            recursive_process_template_strings(template, 'jinja2', template_vars),
            {
                "a": "0.5"
            }
        )

    def test_07(self):
        template = {
            "a": "{{ (numerator / denominator) | float }}"
        }
        template_vars = {
            "numerator": 1,
            "denominator": 2,
        }
        self.assertEqual(
            recursive_process_template_strings(template, 'jinja2', template_vars),
            {
                "a": 0.5
            }
        )

    def test_08(self):
        template = {
            "a": "{{ n }}"
        }
        template_vars = {
            "n": 21,
        }
        self.assertEqual(
            recursive_process_template_strings(template, 'jinja2', template_vars),
            {
                "a": "21"
            }
        )

    def test_09(self):
        template = {
            "a": "{{ n | int }}"
        }
        template_vars = {
            "n": 21
        }
        self.assertEqual(
            recursive_process_template_strings(template, 'jinja2', template_vars),
            {
                "a": 21
            }
        )

    def test_10(self):
        template = {
            "user": "{{ user }}"
        }
        template_vars = {
            "user": {
                "name": "alice"
            }
        }
        self.assertEqual(
            recursive_process_template_strings(template, 'jinja2', template_vars),
            {
                "user": "{'name': 'alice'}"
            }
        )

    def test_11(self):
        template = {
            "user": "{{ user | object }}"
        }
        template_vars = {
            "user": {
                "name": "alice"
            }
        }
        self.assertEqual(
            recursive_process_template_strings(template, 'jinja2', template_vars),
            {
                "user": {
                    "name": "alice"
                }
            }
        )

    def test_12(self):
        template = {
            "now": "{{ now() }}"
        }
        template_vars = {}
        template_out = recursive_process_template_strings(template, 'jinja2', template_vars)
        self.assertRegex(template_out['now'], r'^\d\d\d\d-\d\d-\d\d \d\d:\d\d:\d\d\.\d+$')

    def test_13(self):
        template = {
            "now": "{{ now(True, '%FT%TZ') }}"
        }
        template_vars = {}
        template_out = recursive_process_template_strings(template, 'jinja2', template_vars)
        self.assertRegex(template_out['now'], r'^\d\d\d\d-\d\d-\d\dT\d\d:\d\d:\d\dZ$')

    def test_14(self):
        template = {
            "ts": "{{ (now() + timedelta(hours=3)).strftime('%FT%TZ') }}"
        }
        template_vars = {}
        template_out = recursive_process_template_strings(template, 'jinja2', template_vars)
        self.assertRegex(template_out['ts'], r'^\d\d\d\d-\d\d-\d\dT\d\d:\d\d:\d\dZ$')

    def test_15(self):
        template = {
            "ts": "{{ (now() + timedelta(hours=3)).strftime('%FT%TZ') }}"
        }
        template_vars = {}
        template_out = recursive_process_template_strings(template, 'jinja2', template_vars)
        self.assertRegex(template_out['ts'], r'^\d\d\d\d-\d\d-\d\dT\d\d:\d\d:\d\dZ$')

    def test_16(self):
        template = {
            "ts": "{{ (datetime.now(timezone.utc) + timedelta(hours=3)).strftime('%FT%TZ') }}"
        }
        template_vars = {}
        template_out = recursive_process_template_strings(template, 'jinja2', template_vars)
        self.assertRegex(template_out['ts'], r'^\d\d\d\d-\d\d-\d\dT\d\d:\d\d:\d\dZ$')

    def test_17(self):
        template = {
            "ts": "{{ (datetime.now(timezone.utc) + '3h' | parse_time_interval).strftime('%FT%TZ') }}"
        }
        template_vars = {}
        template_out = recursive_process_template_strings(template, 'jinja2', template_vars)
        self.assertRegex(template_out['ts'], r'^\d\d\d\d-\d\d-\d\dT\d\d:\d\d:\d\dZ$')

    def test_18(self):
        template = {
            "ts1": "{{ timestamp.add('3h') }}",
            "ts2": "{{ (datetime.now(timezone.utc) + timedelta(hours=3)).strftime('%FT%TZ') }}"
        }
        template_vars = {}
        template_out = recursive_process_template_strings(template, 'jinja2', template_vars)
        self.assertRegex(template_out['ts1'], r'^\d\d\d\d-\d\d-\d\dT\d\d:\d\d:\d\dZ$')
        self.assertEqual(template_out['ts1'], template_out['ts2'])

    def test_19(self):
        template = {
            "ts": "{{ timestamp('1970-01-01T01:02:03Z').add('3h') }}",
        }
        template_vars = {}
        template_out = recursive_process_template_strings(template, 'jinja2', template_vars)
        self.assertEqual(template_out['ts'], '1970-01-01T04:02:03Z')

    def test_20(self):
        template = "{{ user | json_query('name') }}"
        template_vars = {
            "user": {
                "name": "alice"
            }
        }
        self.assertEqual(
            recursive_process_template_strings(template, 'jinja2', template_vars), "alice"
        )

    def test_21(self):
        template = "{{ users | json_query('[].name') | object }}"
        template_vars = {
            "users": [
                {
                    "name": "alice",
                    "age": 120,
                }, {
                    "name": "bob",
                    "age": 100,
                }
            ]
        }
        self.assertEqual(
            recursive_process_template_strings(template, 'jinja2', template_vars), ["alice", "bob"]
        )

    # Test complicated case used to determine desired state in babylon governor
    def test_22(self):
        template = """
        {%- if 0 < resource_states | map('default', {}, True) | list | json_query("length([?!contains(keys(status.towerJobs.provision || `{}`), 'completeTimestamp')])") -%}
        {#- desired_state started until all AnarchySubjects have finished provision -#}
        started
        {%- elif 0 < resource_templates | json_query("length([?spec.vars.action_schedule.start < '" ~ now(True, "%FT%TZ") ~ "' && spec.vars.action_schedule.stop > '" ~ now(True, "%FT%TZ") ~ "'])") -%}
        {#- desired_state started for all if any should be started as determined by action schedule -#}
        started
        {%- elif 0 < resource_templates | json_query("length([?spec.vars.default_desired_state == 'started' && !(spec.vars.action_schedule.start || spec.vars.action_schedule.stop)])") -%}
        {#- desired_state started for all if any should be started as determined by default_desired_state -#}
        started
        {%- else -%}
        stopped
        {%- endif -%}
        """
        template_vars = {
            "resource_states": [
                {
                    "status": {
                        "towerJobs": {
                            "provision": {
                                "completeTimestamp": "2022-01-01T00:00:00Z"
                            }
                        }
                    }
                },
                {
                    "status": {
                        "towerJobs": {
                            "provision": {
                                "completeTimestamp": "2022-01-01T00:00:00Z"
                            }
                        }
                    }
                }
            ],
            "resource_templates": [
                {
                    "spec": {
                        "vars": {
                            "action_schedule": {
                                "start": "2022-01-01T00:00:00",
                                "stop": "2022-01-01T00:00:01",
                            }
                        }
                    }
                },
                {
                    "spec": {
                        "vars": {
                            "action_schedule": {
                                "start": "2022-01-01T00:00:00",
                                "stop": "2022-01-01T00:00:01",
                            }
                        }
                    }
                }
            ]
        }

        self.assertEqual(
            recursive_process_template_strings(template, 'jinja2', template_vars), "stopped"
        )

        template_vars['resource_templates'][0]['spec']['vars']['action_schedule']['stop'] = '2099-12-31T23:59:59Z'
        self.assertEqual(
            recursive_process_template_strings(template, 'jinja2', template_vars), "started"
        )

        template_vars['resource_templates'][0]['spec']['vars']['action_schedule']['stop'] = '2022-01-01T00:00:00Z'
        del template_vars['resource_states'][1]['status']['towerJobs']['provision']['completeTimestamp']
        self.assertEqual(
            recursive_process_template_strings(template, 'jinja2', template_vars), "started"
        )

        del template_vars['resource_states'][1]['status']['towerJobs']['provision']
        self.assertEqual(
            recursive_process_template_strings(template, 'jinja2', template_vars), "started"
        )

    def test_23(self):
        self.assertEqual(
            seconds_to_interval(seconds=600.0),
            "10m",
        )

    def test_24(self):
        template = {
            "a": "A",
            "b": "{{ omit }}",
        }
        template_vars = {}
        self.assertEqual(
            recursive_process_template_strings(template, 'jinja2', template_vars), {"a": "A"}
        )

    def test_25(self):
        template = [
            "a", "{{ omit }}", "b"
        ]
        template_vars = {}
        self.assertEqual(
            recursive_process_template_strings(template, 'jinja2', template_vars), ["a", "b"]
        )

    def test_26(self):
        template = {
            "a": "{{ a | default(omit) }}",
            "b": "{{ b | default(omit) }}",
        }
        template_vars = {
            "a": "A",
        }
        self.assertEqual(
            recursive_process_template_strings(template, 'jinja2', template_vars), {"a": "A"}
        )

    def test_27(self):
        template = "{{ l | merge_list_of_dicts | object }}"
        template_vars = {
            "l": [{"a": "A", "b": "X"}, {"b": "B", "c": "C"}, {"d": "D"}]
        }
        self.assertEqual(
            recursive_process_template_strings(template, 'jinja2', template_vars), {"a": "A", "b": "B", "c": "C", "d": "D"}
        )

if __name__ == '__main__':
    unittest.main()
