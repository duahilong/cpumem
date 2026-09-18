# -*- coding: utf-8 -*-
"""新模型快速测试：连通性 + 图片请求 + 耗时"""
import json, sys, urllib.request, time, base64, glob, os
sys.stdout.reconfigure(encoding='utf-8')

BASE = os.path.dirname(os.path.abspath(__file__))
cfg = json.load(open(os.path.join(BASE, 'llm_config.json'), encoding='utf-8'))
img_dir = os.path.join(os.path.dirname(BASE), '价格图片')
imgs = glob.glob(os.path.join(img_dir, '**', '*.png'), recursive=True)
img_path = [p for p in imgs if '0b5aad69ce86dcdee009ef4e0e0fa430' in p][0]

with open(img_path, 'rb') as f:
    b64 = base64.b64encode(f.read()).decode()

body = {'model': cfg['model'], 'max_tokens': 8000, 'messages': [{'role': 'user', 'content': [
    {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + b64}},
    {'type': 'text', 'text': '这张图里第一个产品的型号和价格是什么？简单回答'}]}]}
req = urllib.request.Request(cfg['base_url'].rstrip('/') + '/chat/completions',
    data=json.dumps(body).encode(),
    headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + cfg['api_key']})
t0 = time.time()
r = json.load(urllib.request.urlopen(req, timeout=900))
msg = r['choices'][0]['message']
print('耗时:', f'{time.time()-t0:.0f}s')
print('content:', repr(msg.get('content'))[:300])
print('reasoning:', repr(msg.get('reasoning'))[:300])
print('finish:', r['choices'][0].get('finish_reason'), '| usage:', r['usage'].get('completion_tokens'), 'tokens')
