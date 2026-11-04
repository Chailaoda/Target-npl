import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from content_screening.normalization import normalize
from content_screening.rules_v2 import load_policy, local_analyze, apply_decision
from content_screening import workflow


class ReviewRegressions(unittest.TestCase):
    def setUp(self):
        self.policy = load_policy(Path(__file__).resolve().parents[1] / 'profiles/default.json')

    def decide(self, text):
        record = normalize({'platform': 'douyin', 'content_id': 'fixture', 'text': text})
        return apply_decision(local_analyze(record, self.policy), record, self.policy)

    def test_negation_object_and_real_cancellation(self):
        for text in ('示例活动不想一个人去，找搭子一起打球', '不想再一个人打示例活动，找搭子一起', '示例活动找搭子，不想被黄牛骗'):
            with self.subTest(text=text):
                self.assertTrue(self.decide(text)['accepted'])
        for text in ('示例活动找搭子，算了不去了', '示例活动找搭子，不想去了', '示例活动找搭子，退票去不了'):
            with self.subTest(text=text):
                self.assertFalse(self.decide(text)['accepted'])

    def test_main_workflow_accepts_101_and_500_without_network(self):
        for budget in (101, 500):
            with self.subTest(budget=budget), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                source = root / 'raw.jsonl'
                source.write_text(json.dumps({'platform': 'douyin', 'content_id': 'fixture', 'text': '示例活动找搭子'}), encoding='utf-8')
                policy = root / 'policy.json'
                policy.write_text(json.dumps(self.policy), encoding='utf-8')
                with patch('content_screening.connectors.load_connection', return_value={'ark': {'model': 'fixture', 'base_url': 'fixture'}}):
                    result = workflow.run(source, policy, root / 'db.sqlite3', root / 'output', mode='ark', max_calls=budget,
                                          analyzer=lambda record, context: local_analyze(record, self.policy, context))
                self.assertEqual(result['status'], 'completed')
                self.assertEqual(result['api_calls'], 1)


if __name__ == '__main__':
    unittest.main()
