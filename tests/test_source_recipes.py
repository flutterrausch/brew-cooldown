"""Optional real Homebrew recipe checks; no packages are installed or downloaded."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from brew_cooldown import Brew, CooldownError


@unittest.skipUnless(os.environ.get("BREW_COOLDOWN_LIVE_TESTS"), "opt-in Homebrew Ruby checks")
class SourceRecipeTests(unittest.TestCase):
    def inspect(self, body):
        brew = Brew()
        run = brew.run
        with patch.object(brew, "run", return_value='BREW_COOLDOWN_SOURCE=[]') as capture:
            brew.source_deps("homebrew/core/cooldownfixture")
        script = capture.call_args.args[2]
        recipe = '''class Cooldownfixture < Formula
  url "https://example.invalid/main-1.0.tar.gz"
  sha256 "%s"
  %s
end
''' % ("a" * 64, body)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cooldownfixture.rb"
            loader = 'f = Formulary.from_contents("cooldownfixture", Pathname.new(%s), %s)' % (
                json.dumps(str(path)), json.dumps(recipe))
            script = "\n".join(loader if line.startswith("f = Formulary.factory(") else line
                               for line in script.splitlines())
            return run("ruby", "-e", script)

    def test_resource_adds_implicit_extractor(self):
        result = self.inspect('resource "helper" do\n url "https://example.invalid/helper-1.0.lha"\n'
                              ' sha256 "' + "b" * 64 + '"\nend')
        self.assertIn('"lha"', result)

    def test_unchecksummed_resource_is_rejected(self):
        with self.assertRaisesRegex(CooldownError, "Unverifiable resource"):
            self.inspect('resource "helper" do\n url "https://example.invalid/helper-1.0.tar.gz"\nend')

    def test_unchecksummed_external_patch_is_rejected(self):
        with self.assertRaisesRegex(CooldownError, "Unverifiable resource"):
            self.inspect('patch do\n url "https://example.invalid/fix.patch"\nend')
