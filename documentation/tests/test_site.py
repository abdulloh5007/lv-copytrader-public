import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from html.parser import HTMLParser

DOCS=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(DOCS))
import build
from content import UI,PAGES,LOCALES


class References(HTMLParser):
    def __init__(self):super().__init__();self.references=[];self.ids=set()
    def handle_starttag(self,tag,attrs):
        value=dict(attrs)
        if 'id' in value:self.ids.add(value['id'])
        for key in ('href','src'):
            if key in value:self.references.append(value[key])


class SiteTests(unittest.TestCase):
    def test_every_page_has_localised_content_and_valid_local_assets_links(self):
        with tempfile.TemporaryDirectory() as directory:
            out=Path(directory);build.build(out)
            for lang in LOCALES:
                index=json.loads((out/f'search-{lang}.json').read_text(encoding='utf-8'))
                self.assertEqual(len(index),len(PAGES))
                for slug in ['',*(p['slug'] for p in PAGES)]:
                    file=out/lang/slug/'index.html';text=file.read_text(encoding='utf-8')
                    self.assertIn(f'<html lang="{lang}">',text)
                    parser=References();parser.feed(text)
                    for link in parser.references:
                        if link.startswith('#'):self.assertIn(link[1:],parser.ids,(file,link))
                        elif link.startswith('/'):
                            target=out/link.lstrip('/')
                            if target.is_dir():target=target/'index.html'
                            self.assertTrue(target.is_file(),(file,link))
            for labels in UI.values():self.assertEqual(set(labels),set(LOCALES))
            self.assertTrue((out/'assets/social.png').is_file())

    def test_commands_use_the_official_https_host_and_correct_shell(self):
        self.assertTrue(build.COMMANDS['linux'].startswith('curl -fsSL https://docs.lvenc.site/install.sh'))
        self.assertIn('curl.exe',build.COMMANDS['windows'])
        self.assertTrue(build.COMMANDS['windows'].endswith('| Out-String | iex'))
        self.assertEqual(build.COMMANDS['linux'],build.COMMANDS['macos'])
