import unittest

from greeting import greeting, remember


class GreetingTests(unittest.TestCase):
    def test_greeting(self):
        self.assertEqual(greeting("Ada"), "Hello, Ada!")

    def test_explicit_history(self):
        history = []
        self.assertEqual(remember("Ada", history), ["Ada"])


if __name__ == "__main__":
    unittest.main()
