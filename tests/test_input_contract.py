import unittest

from input_audit import inspect_inputs


class InputContractTests(unittest.TestCase):
    def test_all_declared_inputs_can_be_read(self):
        counts = inspect_inputs()
        self.assertTrue(counts)
        self.assertTrue(all(count > 0 for count in counts.values()))


if __name__ == "__main__":
    unittest.main()
