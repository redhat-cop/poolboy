#!/usr/bin/env python

import unittest
import sys
sys.path.append('../operator')

from gpte.kubeoperative import jsonpatch_from_diff

class TestJsonPatch(unittest.TestCase):
    def test_00(self):
        a = {}
        b = {'foo': 'bar'}
        self.assertEqual(jsonpatch_from_diff(a, b), [{'op': 'add', 'path': '/foo', 'value': 'bar'}])

    def test_01(self):
        a = {'foo': 'bar'}
        b = {}
        self.assertEqual(jsonpatch_from_diff(a, b), [{'op': 'remove', 'path': '/foo'}])

    def test_02(self):
        a = {'foo': 'boo'}
        b = {'foo': 'bar'}
        self.assertEqual(jsonpatch_from_diff(a, b), [{'op': 'replace', 'path': '/foo', 'value': 'bar'}])

    def test_03(self):
        a = []
        b = ['foo']
        self.assertEqual(jsonpatch_from_diff(a, b), [{'op': 'add', 'path': '/0', 'value': 'foo'}])

    def test_04(self):
        a = ['foo']
        b = []
        self.assertEqual(jsonpatch_from_diff(a, b), [{'op': 'remove', 'path': '/0'}])

    def test_04(self):
        a = ['boo']
        b = ['foo']
        self.assertEqual(jsonpatch_from_diff(a, b), [{'op': 'replace', 'path': '/0', 'value': 'foo'}])

    def test_05(self):
        a = {'changeme': 'a', 'removeme': 'a'}
        b = {'changeme': 'b', 'addme': 'b'}
        self.assertEqual(jsonpatch_from_diff(a, b), [
            {'op': 'replace', 'path': '/changeme', 'value': 'b'},
            {'op': 'remove', 'path': '/removeme'},
            {'op': 'add', 'path': '/addme', 'value': 'b'}
        ])

    def test_06(self):
        a = ['a', 'c']
        b = ['b']
        self.assertEqual(jsonpatch_from_diff(a, b), [
            {'op': 'replace', 'path': '/0', 'value': 'b'},
            {'op': 'remove', 'path': '/1'}
        ])

    def test_07(self):
        a = ['a']
        b = ['b', 'c']
        self.assertEqual(jsonpatch_from_diff(a, b), [
            {'op': 'replace', 'path': '/0', 'value': 'b'},
            {'op': 'add', 'path': '/1', 'value': 'c'}
        ])

    def test_07_1(self):
        a = ['a', 'b']
        b = ['b', 'a']
        self.assertEqual(jsonpatch_from_diff(a, b), [
            {'op': 'replace', 'path': '/0', 'value': 'b'},
            {'op': 'replace', 'path': '/1', 'value': 'a'}
        ])

    def test_08(self):
        a = {'a': 'nocopy'}
        b = {'b': 'nocopy'}
        self.assertEqual(jsonpatch_from_diff(a, b), [
            {'op': 'remove', 'path': '/a'},
            {'op': 'add', 'path': '/b', 'value': 'nocopy'}
        ])

    def test_09(self):
        a = {'context': {'a': 'nocopy'}}
        b = {'context': {'b': 'nocopy'}}
        self.assertEqual(jsonpatch_from_diff(a, b), [
            {'op': 'remove', 'path': '/context/a'},
            {'op': 'add', 'path': '/context/b', 'value': 'nocopy'}
        ])

    def test_10(self):
        a = {}
        b = {"foo": "foo/bar~boo"}
        self.assertEqual(jsonpatch_from_diff(a, b), [
            {'op': 'add', 'path': '/foo', 'value': 'foo/bar~boo'}
        ])

    # nested
    def test_11(self):
        a = {'a': {'changeme':1}, 'b':{'removeme': 'foo', 'keep':'bar'}}
        b = {'a': {'changeme':2}, 'b':{'keep':'bar','addme': 'b'}}
        self.assertEqual(jsonpatch_from_diff(a, b), [
            {'op': 'replace', 'path': '/a/changeme', 'value': 2},
            {'op': 'remove', 'path': '/b/removeme'},
            {'op': 'add', 'path': '/b/addme', 'value': 'b'}
        ])

    def test_12(self):
        a = {'removeme': {'a':1, 'c':{'a':[1,2]}}}
        b = {'addme': {'a':1, 'c':{'a':[1,2]}}}
        self.assertEqual(jsonpatch_from_diff(a, b), [
            {'op': 'remove', 'path': '/removeme'},
            {'op': 'add', 'path': '/addme', 'value': {'a':1,'c':{'a':[1,2]}}}
        ])

    def test_13(self):
        a = {'a': {'s':'v', 'b':{'c':[1,2]}}}
        b = {'a': {'s':'v', 'b':{'c':[2,1]}}}
        self.assertEqual(jsonpatch_from_diff(a, b), [
            {'op': 'replace', 'path': '/a/b/c/0', 'value': 2},
            {'op': 'replace', 'path': '/a/b/c/1', 'value': 1}
        ])

    def test_14(self):
        a = {'a': {'s':'v', 'b':{'c':[1,{'changeme':'foo'}]}}}
        b = {'a': {'s':'v', 'b':{'c':[1,{'changeme':'bar'}]}}}
        self.assertEqual(jsonpatch_from_diff(a, b), [
            {'op': 'replace', 'path': '/a/b/c/1/changeme', 'value': 'bar'}
        ])

    def test_15(self):
        # update an array inside a dictionary inside an array
        a = [{'a':1,'b':2,'c':{'foo':'bar'},'d':[1,2,3]}]
        b = [{'d':[1,2,3,4]}]
        self.assertEqual(jsonpatch_from_diff(a, b), [
            {'op': 'remove', 'path': '/0/a'},
            {'op': 'remove', 'path': '/0/b'},
            {'op': 'remove', 'path': '/0/c'},
            {'op': 'add', 'path': '/0/d/3', 'value':4},
        ])

if __name__ == '__main__':
    unittest.main()
