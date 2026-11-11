"""Exercise the actual Swift delegate offline; never invoke Keychain or network."""
from pathlib import Path
import os
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
let valid = #"{"model":"jev-1.14.0","answers":{"connection_check":{"type":"choice","choice":"probe","probabilities":{"probe":1}}}}"#
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
expect(exercise(valid.replacingOccurrences(of:"jev-1.14.0", with:"jev-1.13.0")) == "invalid-response", "wrong served model")
expect(exercise(valid.replacingOccurrences(of:"jev-1.14.0", with:"jev-latest")) == "invalid-response", "served alias refused")
expect(exercise(valid.replacingOccurrences(of:"\"model\":\"jev-1.14.0\",", with:"")) == "invalid-response", "missing served model")
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
print("PASS: 19 native delegate checks; no Keychain or network calls")
'''
PIN = 'jev-1.14.0'


class JevPinTests(unittest.TestCase):
    """The helper requests the pinned Jev release and rejects any other served model (PLAN A4, M1).
    Source-level, so it runs without a Swift toolchain; the native tests below exercise the delegate."""

    def test_request_pins_the_release_and_never_the_floating_alias(self):
        source = SOURCE.read_text()
        self.assertNotIn('jev-latest', source)
        self.assertEqual(source.count(f'"model": "{PIN}",'), 1)
        request = source[source.index('func checkConnection()'):]
        self.assertIn(f'"model": "{PIN}"', request[:request.index('URLRequest(')])

    def test_response_must_name_the_same_served_model(self):
        source = SOURCE.read_text()
        delegate = source[source.index('didCompleteWithError'):source.index('func checkConnection()')]
        self.assertEqual(delegate.count(f'body["model"] as? String == "{PIN}"'), 1)
        self.assertLess(delegate.index(f'body["model"] as? String == "{PIN}"'), delegate.index('state = "connected"'))


@unittest.skipUnless(platform.system() == 'Darwin' and shutil.which('xcrun'), 'Requires macOS Swift toolchain')
class NativeJevTests(unittest.TestCase):
    def test_public_cli_receipts_offline(self):
        source = SOURCE.read_text()
        # Substitute only the Keychain and HTTPS boundaries; run the real CLI
        # dispatch and serializer. Never invoke configure or a native key lookup.
        start = source.index('func lookup(data:')
        end = source.index('\n}\n', start) + 3
        source = source[:start] + '''
func lookup(data: Bool) -> (OSStatus, CFTypeRef?) {
    let status = ProcessInfo.processInfo.environment["TEST_STATUS"] ?? "saved"
    let attributes: [String: Any] = [kSecAttrCreationDate as String: Date(timeIntervalSinceReferenceDate: 0),
                                    kSecAttrModificationDate as String: Date(timeIntervalSinceReferenceDate: 1)]
    return (status == "saved" ? errSecSuccess : status == "absent" ? errSecItemNotFound : errSecAuthFailed,
            attributes as CFDictionary)
}
''' + source[end:]
        start = source.index('func checkConnection()')
        end = source.index(MARKER, start)
        source = source[:start] + '''
func checkConnection() -> String {
    return ProcessInfo.processInfo.environment["TEST_STATE"] ?? "connected"
}
''' + source[end:]
        with tempfile.TemporaryDirectory(prefix='agiw-jev-cli-test-') as directory:
            path = Path(directory)
            swift = path / 'main.swift'
            swift.write_text(source)
            executable = path / 'test-cli'
            compiled = subprocess.run(['xcrun', 'swiftc', str(swift), '-o', str(executable)],
                                      capture_output=True, text=True, timeout=60)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            cases = [
                ('status', {'TEST_STATUS': 'saved'}, b'{"available":true,"configured":true}\n', 0),
                ('status', {'TEST_STATUS': 'absent'}, b'{"available":true,"configured":false}\n', 0),
                ('status', {'TEST_STATUS': 'locked'}, b'{"available":false,"configured":false}\n', 0),
                ('unknown', {}, b'', 2),
                ('status-revision', {'TEST_STATUS': 'saved'},
                 b'{"available":true,"configured":true,"revision":"legacy:0:3ff0000000000000"}\n', 0),
                ('status-revision', {'TEST_STATUS': 'absent'},
                 b'{"available":true,"configured":false,"revision":null}\n', 0),
                ('status-revision', {'TEST_STATUS': 'locked'},
                 b'{"available":false,"configured":false,"revision":null}\n', 0),
            ]
            for state in ('connected', 'not-configured', 'auth-failed', 'rate-limited', 'network-error',
                          'invalid-response', 'unavailable', 'error'):
                cases.append(('check', {'TEST_STATE': state}, ('{"state":"' + state + '"}\n').encode(), 0))
            for command, env, output, code in cases:
                with self.subTest(command=command, env=env):
                    result = subprocess.run([str(executable), command], env={**os.environ, **env},
                                            capture_output=True, timeout=10)
                    self.assertEqual((result.returncode, result.stdout, result.stderr), (code, output, b''))

    def test_credential_revision_and_saved_bytes_offline(self):
        source = SOURCE.read_text()
        # Execute the actual save-value construction, but never SecItemUpdate/Add.
        values = source[source.index('        let values:'):source.index('        var status = SecItemUpdate')]
        harness = '''
func saveValues() -> [String: Any] {
    let key = "fixture-key-only"
''' + values + '''
    return values
}
func expect(_ condition: Bool) {
    if !condition { fputs("FAILED: credential association metadata\\n", stderr); exit(1) }
}
let first = saveValues(), second = saveValues()
expect(Set(first.keys) == Set([kSecValueData as String, kSecAttrGeneric as String]))
expect(first[kSecValueData as String] as? Data == Data("fixture-key-only".utf8))
let firstGeneration = String(data: first[kSecAttrGeneric as String] as! Data, encoding: .utf8)!
expect(UUID(uuidString: firstGeneration) != nil)
expect(first[kSecAttrGeneric as String] as? Data != second[kSecAttrGeneric as String] as? Data)
var attributes: [String: Any] = [kSecAttrCreationDate as String: Date(timeIntervalSinceReferenceDate: 0),
                               kSecAttrModificationDate as String: Date(timeIntervalSinceReferenceDate: 1)]
let legacy = credentialRevision(attributes as CFDictionary)
expect(legacy == "legacy:0:3ff0000000000000")
expect(credentialRevision(attributes as CFDictionary) == legacy)
attributes[kSecAttrGeneric as String] = first[kSecAttrGeneric as String]
let saved = credentialRevision(attributes as CFDictionary)
expect(saved != legacy)
attributes[kSecAttrGeneric as String] = second[kSecAttrGeneric as String]
expect(credentialRevision(attributes as CFDictionary) != saved)
attributes.removeValue(forKey: kSecAttrGeneric as String)
attributes[kSecAttrModificationDate as String] = Date(timeIntervalSinceReferenceDate: 2)
expect(credentialRevision(attributes as CFDictionary) != legacy)
expect(credentialRevision(nil) == nil)
expect(credentialRevision([:] as CFDictionary) == nil)
print("PASS: 11 credential metadata checks; no Keychain or network calls")
'''
        with tempfile.TemporaryDirectory(prefix='agiw-jev-revision-test-') as directory:
            path = Path(directory)
            swift = path / 'main.swift'
            swift.write_text(source.split(MARKER)[0] + harness)
            executable = path / 'test-revision'
            compiled = subprocess.run(['xcrun', 'swiftc', str(swift), '-o', str(executable)],
                                      capture_output=True, text=True, timeout=60)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), 'PASS: 11 credential metadata checks; no Keychain or network calls')

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
            self.assertEqual(result.stdout.strip(), 'PASS: 19 native delegate checks; no Keychain or network calls')


if __name__ == '__main__':
    unittest.main()
