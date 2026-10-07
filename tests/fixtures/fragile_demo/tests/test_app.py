import unittest

from app import append_item, greeting


class BehaviorTests(unittest.TestCase):
    def test_greeting(self):
        self.assertEqual(greeting(), "Hello from CodeProof")

    def test_explicit_list_is_preserved(self):
        existing = [1]
        self.assertIs(append_item(2, existing), existing)
        self.assertEqual(existing, [1, 2])
