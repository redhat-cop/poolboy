#!/usr/bin/env python3

import unittest
import sys
sys.path.append('../operator')

from gpte.util import recursive_process_template_strings

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

if __name__ == '__main__':
    unittest.main()
