"""Exercise the actual TypeScript worker without a provider or paid request."""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from proactive_sdk.pi_worker import PiWorkerConfig, PiWorkerExecutor


@unittest.skipUnless(shutil.which("node"), "Node is not installed")
class PiAnalysisSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_analysis_prompt_replaces_discovery_and_disables_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "dist").mkdir()
            (root / "package.json").write_text(json.dumps({"type": "module", "version": "test"}))
            entry = root / "dist/index.js"
            entry.write_text('''
export const getAgentDir = () => "/unused";
export const SessionManager = {inMemory: () => ({})};
export class DefaultResourceLoader {
  constructor(options) { this.options = options; }
  async reload() { this.loaded = true; }
}
export async function createAgentSession(options) {
  const loader = options.resourceLoader;
  if (!loader.loaded || !loader.options.systemPrompt.includes("raw decision JSON") ||
      !loader.options.noSkills || !loader.options.noExtensions ||
      !loader.options.noContextFiles || options.tools.length !== 0) throw Error("unsafe analysis session");
  return {session: {
    async prompt() {}, async waitForIdle() {}, async abort() {}, dispose() {},
    isIdle: () => true, getActiveToolNames: () => [],
    getLastAssistantText: () => JSON.stringify({decision:"silent", summary:"test", proposals:[]}),
    getSessionStats: () => ({toolCalls:0,tokens:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}})
  }};
}
''')
            executor = PiWorkerExecutor(PiWorkerConfig(
                command=(shutil.which("node"), "--experimental-strip-types", str(ROOT / "src/proactive_sdk/pi_worker/pi_worker.ts")),
                pi_entry=str(entry), allowed_tools=(),
            ))
            try:
                await executor.start()
                reply = await executor.run(run_id="test-analysis", instruction="return silent", cwd=tmp)
                self.assertEqual(reply.envelope["decision"], "silent")
            finally:
                await executor.close()


if __name__ == "__main__":
    unittest.main()
