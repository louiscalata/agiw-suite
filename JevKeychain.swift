// Copyright 2026 Louis Calata. SPDX-License-Identifier: Apache-2.0
import Cocoa
import Security
import LocalAuthentication

let service = "com.louiscalata.agiw.jev"
let account = "typesafe"
let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
                            kSecAttrService as String: service,
                            kSecAttrAccount as String: account]

func emit(_ value: [String: Any]) {
    if let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]) {
        FileHandle.standardOutput.write(data)
        FileHandle.standardOutput.write(Data("\n".utf8))
    }
}

func lookup(data: Bool) -> (OSStatus, CFTypeRef?) {
    var request = query
    let context = LAContext()
    context.interactionNotAllowed = true
    request[kSecUseAuthenticationContext as String] = context
    request[kSecMatchLimit as String] = kSecMatchLimitOne
    request[(data ? kSecReturnData : kSecReturnAttributes) as String] = true
    var result: CFTypeRef?
    let status = SecItemCopyMatching(request as CFDictionary, &result)
    return (status, result)
}

// This delegate accepts only the fixed provider response. It never exports a
// credential, provider error body, or arbitrary response text to the observer.
final class JevProbe: NSObject, URLSessionDataDelegate {
    let done = DispatchSemaphore(value: 0)
    var state = "pending"
    private var bytes = Data()
    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest,
                    completionHandler: @escaping (URLRequest?) -> Void) {
        state = "invalid-response"
        completionHandler(nil)
    }
    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask,
                    didReceive response: URLResponse,
                    completionHandler: @escaping (URLSession.ResponseDisposition) -> Void) {
        guard let http = response as? HTTPURLResponse,
              http.url?.absoluteString == "https://api.typesafe.ai/v1/systemone" else {
            state = "invalid-response"; completionHandler(.cancel); return
        }
        guard http.statusCode == 200 else {
            state = [401, 403].contains(http.statusCode) ? "auth-failed" : http.statusCode == 429 ? "rate-limited" : "network-error"
            completionHandler(.cancel); return
        }
        guard response.expectedContentLength <= 65_536,
              response.mimeType == "application/json" else {
            state = "invalid-response"; completionHandler(.cancel); return
        }
        completionHandler(.allow)
    }
    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        guard bytes.count + data.count <= 65_536 else {
            state = "invalid-response"; dataTask.cancel(); return
        }
        bytes.append(data)
    }
    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        defer { done.signal() }
        guard state == "pending" else { return }
        guard error == nil else { state = "network-error"; return }
        guard let body = (try? JSONSerialization.jsonObject(with: bytes)) as? [String: Any],
              let answers = body["answers"] as? [String: Any],
              let answer = answers["connection_check"] as? [String: Any],
              answer["type"] as? String == "choice", answer["choice"] as? String == "probe",
              let probabilities = answer["probabilities"] as? [String: Any],
              Set(probabilities.keys) == Set(["probe"]),
              let probability = probabilities["probe"] as? NSNumber,
              CFGetTypeID(probability) != CFBooleanGetTypeID(),
              probability.doubleValue.isFinite, (0...1).contains(probability.doubleValue) else {
            state = "invalid-response"; return
        }
        state = "connected"
    }
}

func checkConnection() -> String {
    let (status, value) = lookup(data: true)
    guard status == errSecSuccess, let data = value as? Data,
          (10...4096).contains(data.count), let key = String(data: data, encoding: .utf8),
          key.unicodeScalars.allSatisfy({ $0.value >= 33 && $0.value <= 126 }) else {
        return status == errSecItemNotFound ? "not-configured" : "unavailable"
    }
    let body: [String: Any] = [
        "state": "Synthetic connectivity probe. No user or application data.",
        "model": "jev-latest",
        "questions": ["connection_check": ["type": "choice",
            "instructions": "Classify this fixed synthetic connectivity probe.",
            "criteria": ["probe": "A synthetic connectivity probe."]]]
    ]
    var request = URLRequest(url: URL(string: "https://api.typesafe.ai/v1/systemone")!)
    request.httpMethod = "POST"
    request.setValue("Bearer " + key, forHTTPHeaderField: "Authorization")
    request.setValue("application/json", forHTTPHeaderField: "Content-Type")
    request.httpBody = try? JSONSerialization.data(withJSONObject: body, options: [.sortedKeys])
    request.timeoutInterval = 20
    let delegate = JevProbe()
    let configuration = URLSessionConfiguration.ephemeral
    configuration.timeoutIntervalForRequest = 20
    configuration.timeoutIntervalForResource = 20
    configuration.httpShouldSetCookies = false
    configuration.urlCredentialStorage = nil
    let queue = OperationQueue()
    queue.maxConcurrentOperationCount = 1
    let session = URLSession(configuration: configuration, delegate: delegate, delegateQueue: queue)
    session.dataTask(with: request).resume()
    let finished = delegate.done.wait(timeout: .now() + 25) == .success
    session.invalidateAndCancel()
    return finished ? delegate.state : "network-error"
}

let args = Array(CommandLine.arguments.dropFirst())
if args == ["status"] {
    let (status, _) = lookup(data: false)
    emit(["configured": status == errSecSuccess, "available": status == errSecSuccess || status == errSecItemNotFound])
} else if args == ["check"] {
    emit(["state": checkConnection()])
} else if args == ["configure"] {
    let app = NSApplication.shared
    app.setActivationPolicy(.accessory)
    app.activate(ignoringOtherApps: true)
    let alert = NSAlert()
    alert.messageText = "Connect to hosted Jev"
    alert.informativeText = "Enter your own TypeSafe API key. AGIW stores it in this Mac’s login Keychain. Saving it makes no network request. The explicit connection check sends only a fixed synthetic test to TypeSafe; it never sends your files, prompts, or model inventory. Provider usage charges may apply."
    alert.addButton(withTitle: "Save connection")
    alert.addButton(withTitle: "Cancel")
    alert.addButton(withTitle: "Remove saved connection")
    let field = NSSecureTextField(frame: NSRect(x: 0, y: 0, width: 420, height: 28))
    field.placeholderString = "TypeSafe API key"
    alert.accessoryView = field
    alert.window.initialFirstResponder = field
    let response = alert.runModal()
    if response == .alertFirstButtonReturn {
        let key = field.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard key.utf8.count >= 10, key.utf8.count <= 4096,
              key.unicodeScalars.allSatisfy({ $0.value >= 33 && $0.value <= 126 }) else {
            let failure = NSAlert()
            failure.messageText = "The key could not be saved"
            failure.informativeText = "Use a valid TypeSafe API key with no spaces or line breaks. The saved connection was not changed."
            failure.runModal()
            exit(1)
        }
        let values: [String: Any] = [kSecValueData as String: Data(key.utf8)]
        var status = SecItemUpdate(query as CFDictionary, values as CFDictionary)
        if status == errSecItemNotFound {
            var add = query
            add[kSecValueData as String] = Data(key.utf8)
            add[kSecAttrLabel as String] = "AGIW · Jev connection"
            status = SecItemAdd(add as CFDictionary, nil)
        }
        field.stringValue = ""
        if status != errSecSuccess {
            let failure = NSAlert()
            failure.messageText = "Keychain did not save the connection"
            failure.informativeText = "Unlock your login Keychain and try again. No network request was sent."
            failure.runModal()
            exit(1)
        }
        emit(["configured": true])
    } else if response == .alertThirdButtonReturn {
        let confirm = NSAlert()
        confirm.messageText = "Remove AGIW’s Jev connection?"
        confirm.informativeText = "This removes only the API key saved by AGIW. Your external Nisi/Jev router settings are unchanged."
        confirm.addButton(withTitle: "Remove connection")
        confirm.addButton(withTitle: "Cancel")
        if confirm.runModal() == .alertFirstButtonReturn {
            let status = SecItemDelete(query as CFDictionary)
            emit(["configured": !(status == errSecSuccess || status == errSecItemNotFound)])
            if status != errSecSuccess && status != errSecItemNotFound { exit(1) }
        }
    }
} else {
    exit(2)
}
