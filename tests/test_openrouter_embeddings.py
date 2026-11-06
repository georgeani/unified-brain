import json
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import httpx
from openai import OpenAI

import skills_lib
import store
import reembed_code_vectors


class OpenRouterEmbeddingsTest(unittest.TestCase):
    def test_ingest_and_reembed_a_git_repository(self):
        def handle(request):
            payload = json.loads(request.content)
            self.assertEqual(payload['model'], 'qwen/qwen3-embedding-8b')
            data = [{'object': 'embedding', 'index': i, 'embedding': [1.0] + [0.0] * 4095}
                    for i in range(len(payload['input']))]
            return httpx.Response(200, json={'object': 'list', 'model': payload['model'], 'data': data,
                                             'usage': {'prompt_tokens': 1, 'total_tokens': 1}})

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / 'repo'
            repo.mkdir()
            def git(*args):
                subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True)
            git('init')
            git('config', 'user.name', 'Test')
            git('config', 'user.email', 'test@example.invalid')
            (repo / 'demo.py').write_text('def demo():\n    return 42\n')
            git('add', 'demo.py')
            git('commit', '-m', 'Add demo')
            ingester = Path(__file__).resolve().parents[1] / 'next_claude.py'
            result = subprocess.run([sys.executable, str(ingester), str(repo), '--no-embed'],
                                    capture_output=True, text=True, timeout=60, check=True)
            self.assertIn('1 commit(s) ingested', result.stdout)
            client = OpenAI(api_key='test', base_url=skills_lib.OPENROUTER_OPENAI_BASE,
                            http_client=httpx.Client(transport=httpx.MockTransport(handle)))
            db = store.open(str(repo / '.unified-brain.db'))
            try:
                embedder = skills_lib.CodeEmbedder('openrouter', client)
                summary = reembed_code_vectors.run(db, db.binary(), embedder, workers=1)
                self.assertEqual(summary['failed'], 0)
                self.assertGreater(summary['embedded'], 0)
                query = embedder.embed(['where is demo?'], 'query')[0]
                rows = db.knn(embedder.prefix, query, 5, latest_only=True, include_obsolete=False)
                self.assertTrue(any(row['def_key'].startswith('demo.py:demo:') for row in rows), rows)
            finally:
                db.close()
                client.close()

    def test_query_batching_order_and_vector_store(self):
        requests = []

        def handle(request):
            payload = json.loads(request.content)
            requests.append(payload)
            self.assertEqual(request.url.path, '/api/v1/embeddings')
            self.assertEqual(payload['model'], 'qwen/qwen3-embedding-8b')
            rows = [
                {'object': 'embedding', 'index': i, 'embedding': [float(i + 1)] + [0.0] * 4095}
                for i in range(len(payload['input']))
            ]
            return httpx.Response(200, json={
                'object': 'list', 'model': payload['model'],
                'data': list(reversed(rows)), 'usage': {'prompt_tokens': 1, 'total_tokens': 1},
            })

        client = OpenAI(api_key='test', base_url=skills_lib.OPENROUTER_OPENAI_BASE,
                        http_client=httpx.Client(transport=httpx.MockTransport(handle)))
        embedder = skills_lib.CodeEmbedder('openrouter', client)
        vectors = embedder.embed([f'query {i}' for i in range(17)], 'query')
        self.assertEqual([len(r['input']) for r in requests], [16, 1])
        self.assertTrue(requests[0]['input'][0].startswith(skills_lib.QWEN_QUERY_INSTRUCT))
        self.assertEqual(struct.unpack('4096f', vectors[1])[0], 2.0)

        with tempfile.TemporaryDirectory() as tmp:
            database = store.open(str(Path(tmp) / 'test.db'))
            try:
                database.hset('cveco_at:skill:commit:0', mapping={
                    'embedding': vectors[0], 'skill_id': 'skill', 'def_key': 'demo.py:fn:1:function',
                    'from_rank': '0', 'to_rank': '99', 'latest': '1', 'obsolete': '0',
                })
                rows = database.knn('cveco_at', vectors[0], 1, latest_only=True)
                self.assertEqual(rows[0]['skill_id'], 'skill')
                self.assertEqual(database.knn('cvecq_at', vectors[0], 1), [])
            finally:
                database.close()
                client.close()

    def test_rejects_incomplete_or_wrong_dimension_vectors(self):
        for data in ([], [{'object': 'embedding', 'index': 0, 'embedding': [0.1]}]):
            with self.subTest(data=data):
                def handle(request):
                    return httpx.Response(200, json={'object': 'list', 'model': 'qwen/qwen3-embedding-8b',
                                                     'data': data, 'usage': {'prompt_tokens': 1, 'total_tokens': 1}})
                client = OpenAI(api_key='test', base_url=skills_lib.OPENROUTER_OPENAI_BASE,
                                http_client=httpx.Client(transport=httpx.MockTransport(handle)))
                try:
                    with self.assertRaises(ValueError):
                        skills_lib.CodeEmbedder('openrouter', client).embed(['body'], 'document')
                finally:
                    client.close()


if __name__ == '__main__':
    unittest.main()
