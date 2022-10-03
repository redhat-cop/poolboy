#!/usr/bin/env python3

import unittest
import sys
sys.path.append('../operator')

from openapi_schema_util import defaults_from_schema

class TestDefaultsFromSchema(unittest.TestCase):
    def test_00(self):
        schema = {
            "type": "object",
            "properties": {},
        }
        self.assertEqual(
            defaults_from_schema(schema),
            {}
        )

    def test_01(self):
        schema = {
            "type": "object",
            "properties": {
                "foo": {
                    "default": "bar",
                    "type": "string"
                }
            }
        }
        self.assertEqual(
            defaults_from_schema(schema),
            {
                "foo": "bar"
            }
        )

    def test_02(self):
        schema = {
            "type": "object",
            "properties": {
                "foo": {
                    "properties": {
                        "bar": {
                            "default": "a",
                            "type": "string"
                        }
                    },
                    "type": "object"
                }
            }
        }
        self.assertEqual(
            defaults_from_schema(schema),
            {
                "foo": {
                    "bar": "a"
                }
            }
        )

if __name__ == '__main__':
    unittest.main()
