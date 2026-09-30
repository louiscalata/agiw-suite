"""Exercise the actual Swift delegate offline; never invoke Keychain or network."""
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parent / 'JevKeychain.swift'
MARKER = 'let args = Array(CommandLine.arguments.dropFirst())'
HARNESS = r'''
let session = URLSession(configuration: .ephemeral)
let task = session.dataTask(with: URL(string: "https://api.typesafe.ai/v1/systemone")!)
let valid = #"{"answers":{"connection_check":{"type":"choice","choice":"probe","probabilities":{"probe":1}}}}"#
func expect(_ condition: Bool, _ label: String) {
    if !condition { fputs("FAILED: \(label)\n", stderr); exit(1) }
}
func exercise(_ text: String, status: Int = 200, mime: String = "application/json", length: Int = 200,
              url: String = "https://api.typesafe.ai/v1/systemone", error: Error? = nil) -> String {
    let delegate = JevProbe()
    let response = HTTPURLResponse(url: URL(string:url)!, statusCode:status, httpVersion:nil,
        headerFields:["Content-Type":mime,"Content-Length":String(length)])!
    var disposition: URLSession.ResponseDisposition = .cancel
    delegate.urlSession(session, dataTask:task, didReceive:response) { disposition = $0 }
    if disposition == .allow { delegate.urlSession(session, dataTask:task, didReceive:Data(text.utf8)) }
    delegate.urlSession(session, task:task, didCompleteWithError:error)
    return delegate.state
}
expect(exercise(valid) == "connected", "valid answer")
expect(exercise(valid.replacingOccurrences(of:"\"probe\":1", with:"\"probe\":true")) == "invalid-response", "boolean probability")
expect(exercise(valid.replacingOccurrences(of:"\"probe\":1", with:"\"probe\":2")) == "invalid-response", "probability range")
expect(exercise("provider-private-error-text") == "invalid-response", "provider text never exported")
expect(exercise("[]") == "invalid-response", "invalid shape")
expect(exercise(valid, mime:"text/plain") == "invalid-response", "mime")
expect(exercise(valid, length:65537) == "invalid-response", "declared oversized")
expect(exercise(String(repeating:"x",count:65537), length:0) == "invalid-response", "actual oversized")
expect(exercise(valid, url:"https://example.invalid/v1/systemone") == "invalid-response", "wrong endpoint")
expect(exercise(valid, status:401) == "auth-failed", "401")
expect(exercise(valid, status:403) == "auth-failed", "403")
expect(exercise(valid, status:429) == "rate-limited", "429")
expect(exercise(valid, status:500) == "network-error", "500")
expect(exercise(valid, error:NSError(domain:"synthetic-secret-text",code:1)) == "network-error", "transport error sanitized")
let redirect = JevProbe()
var redirected: URLRequest? = URLRequest(url:URL(string:"https://example.invalid")!)
redirect.urlSession(session, task:task, willPerformHTTPRedirection:
    HTTPURLResponse(url:URL(string:"https://api.typesafe.ai/v1/systemone")!,statusCode:302,httpVersion:nil,headerFields:nil)!,
    newRequest:redirected!) { redirected = $0 }
expect(redirected == nil && redirect.state == "invalid-response", "redirect refused")
redirect.urlSession(session, task:task, didCompleteWithError:nil)
expect(redirect.state == "invalid-response", "redirect state retained")
session.invalidateAndCancel()
print("PASS: 16 native delegate checks; no Keychain or network calls")
'''


@unittest.skipUnless(platform.system() == 'Darwin' and shutil.which('xcrun'), 'Requires macOS Swift toolchain')
class NativeJevTests(unittest.TestCase):
    def test_full_source_typechecks(self):
        result = subprocess.run(['xcrun', 'swiftc', '-typecheck', str(SOURCE)], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_actual_delegate_offline(self):
        source = SOURCE.read_text()
        self.assertEqual(source.count(MARKER), 1)
        with tempfile.TemporaryDirectory(prefix='agiw-jev-native-test-') as directory:
            path = Path(directory)
            swift = path / 'main.swift'
            swift.write_text(source.split(MARKER)[0] + HARNESS)
            executable = path / 'test-delegate'
            compile_result = subprocess.run(['xcrun', 'swiftc', str(swift), '-o', str(executable)], capture_output=True, text=True, timeout=60)
            self.assertEqual(compile_result.returncode, 0, compile_result.stderr)
            result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), 'PASS: 16 native delegate checks; no Keychain or network calls')


if __name__ == '__main__':
    unittest.main()
