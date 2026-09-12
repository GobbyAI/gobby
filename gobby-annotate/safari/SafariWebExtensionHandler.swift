import SafariServices

final class SafariWebExtensionHandler: NSObject, NSExtensionRequestHandling {
  func beginRequest(with context: NSExtensionContext) {
    let response = NSExtensionItem()
    do {
      guard let item = context.inputItems.first as? NSExtensionItem,
        let message = item.userInfo?[SFExtensionMessageKey]
      else {
        throw ExportStore.Failure.invalid("Missing native message")
      }
      let bytes = try JSONSerialization.data(withJSONObject: message)
      guard bytes.count <= 300000 else { throw ExportStore.Failure.invalid("Message too large") }
      let request = try JSONDecoder().decode(ExportRequest.self, from: bytes)
      try ExportStore.shared().handle(request)
      response.userInfo = [SFExtensionMessageKey: ["ok": true]]
    } catch {
      response.userInfo = [
        SFExtensionMessageKey: ["ok": false, "error": error.localizedDescription]
      ]
    }
    context.completeRequest(returningItems: [response], completionHandler: nil)
  }
}
