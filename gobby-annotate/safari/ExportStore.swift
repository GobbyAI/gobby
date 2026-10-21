import Darwin
import Foundation

struct ExportRequest: Decodable {
  let action: String
  let transferId: String?
  let name: String?
  let size: Int?
  let offset: Int?
  let data: String?
}

struct ExportStore {
  static let maximumBytes = 128 * 1024 * 1024
  let root: URL
  struct Transfer: Codable {
    let name: String
    let size: Int
  }
  enum Failure: LocalizedError {
    case invalid(String)
    var errorDescription: String? {
      if case .invalid(let message) = self { return message }
      return nil
    }
  }
  static func shared() throws -> ExportStore {
    guard
      let root = FileManager.default.containerURL(
        forSecurityApplicationGroupIdentifier: "group.ai.gobby.annotate")
    else {
      throw Failure.invalid(
        "App Group unavailable. Build and sign the app and extension with group.ai.gobby.annotate enabled."
      )
    }
    return ExportStore(root: root.appendingPathComponent("Exports", isDirectory: true))
  }
  func handle(_ request: ExportRequest) throws {
    guard let transferId = request.transferId, UUID(uuidString: transferId) != nil else {
      throw Failure.invalid("Invalid transfer ID")
    }
    let fm = FileManager.default
    try fm.createDirectory(at: root, withIntermediateDirectories: true)
    let lock = open(root.appendingPathComponent(".lock").path, O_CREAT | O_RDWR | O_NOFOLLOW, 0o600)
    guard lock >= 0 else { throw Failure.invalid("Cannot lock export storage") }
    defer { close(lock) }
    guard flock(lock, LOCK_EX) == 0 else { throw Failure.invalid("Cannot lock export storage") }
    defer { flock(lock, LOCK_UN) }
    let directory = root.appendingPathComponent(transferId, isDirectory: true)
    let partial = directory.appendingPathComponent("data.part")
    let metadata = directory.appendingPathComponent("transfer.json")
    switch request.action {
    case "begin":
      guard let size = request.size, size > 0, size <= Self.maximumBytes,
        let name = request.name,
        name.hasPrefix("gobby-annotate-"), name.hasSuffix(".zip"),
        UUID(uuidString: String(name.dropFirst("gobby-annotate-".count).dropLast(4))) != nil
      else { throw Failure.invalid("Invalid export name or size (maximum 128 MiB)") }
      guard !fm.fileExists(atPath: directory.path) else {
        throw Failure.invalid("Transfer already exists")
      }
      let pending = try fm.contentsOfDirectory(
        at: root, includingPropertiesForKeys: [.isDirectoryKey]
      ).filter { UUID(uuidString: $0.lastPathComponent) != nil }
      guard pending.count < 4 else {
        throw Failure.invalid(
          "Four unfinished transfers exist. Clear interrupted transfers in the app and retry.")
      }
      try fm.createDirectory(at: directory, withIntermediateDirectories: false)
      do {
        try writeReplacing(JSONEncoder().encode(Transfer(name: name, size: size)), to: metadata)
        try Data().write(to: partial, options: .withoutOverwriting)
      } catch {
        try? fm.removeItem(at: directory)
        throw error
      }
    case "chunk":
      let transfer = try JSONDecoder().decode(Transfer.self, from: Data(contentsOf: metadata))
      guard let offset = request.offset, let text = request.data, text.count <= 262144,
        let bytes = Data(base64Encoded: text), !bytes.isEmpty, bytes.count <= 192 * 1024
      else { throw Failure.invalid("Invalid transfer chunk") }
      let file = try FileHandle(forUpdating: partial)
      defer { try? file.close() }
      let end = try file.seekToEnd()
      guard offset >= 0, end == UInt64(offset), offset <= transfer.size - bytes.count else {
        throw Failure.invalid("Chunk out of order or exceeds declared size")
      }
      try file.write(contentsOf: bytes)
      try file.synchronize()
    case "finish":
      let transfer = try JSONDecoder().decode(Transfer.self, from: Data(contentsOf: metadata))
      let size = (try fm.attributesOfItem(atPath: partial.path)[.size] as? NSNumber)?.intValue
      guard size == transfer.size else {
        throw Failure.invalid("Incomplete transfer; export remains hidden")
      }
      let file = try FileHandle(forReadingFrom: partial)
      defer { try? file.close() }
      guard try file.read(upToCount: 4) == Data([0x50, 0x4b, 0x03, 0x04]) else {
        throw Failure.invalid("Export is not a ZIP")
      }
      let destination = root.appendingPathComponent(transfer.name)
      guard !fm.fileExists(atPath: destination.path) else {
        throw Failure.invalid("Export already staged")
      }
      try fm.moveItem(at: partial, to: destination)
      try fm.removeItem(at: directory)
    case "cancel":
      if fm.fileExists(atPath: directory.path) { try fm.removeItem(at: directory) }
    case "delete":
      // Extension unstage removes the ZIP only. The app's deletion record stays until the extension
      // acknowledges it, so a failed batch removal in Safari can retry on the next list.
      try removeStaged(transferId)
    case "acknowledge":
      // Sent only after the Safari batch is gone, or kept as an edited draft.
      try updateDeleted { $0.remove(transferId) }
    default: throw Failure.invalid("Unknown export action")
    }
  }
  func completed() throws -> [URL] {
    guard FileManager.default.fileExists(atPath: root.path) else { return [] }
    return try FileManager.default.contentsOfDirectory(at: root, includingPropertiesForKeys: nil)
      .filter { $0.pathExtension == "zip" }.sorted { $0.lastPathComponent < $1.lastPathComponent }
  }
  /// App-side delete. The deletion is recorded before the ZIP goes: if removal then fails, the
  /// ZIP stays listed and can be deleted again, while a removal without a record would leave the
  /// Safari batch behind.
  func delete(_ file: URL) throws {
    let exportId = String(
      file.deletingPathExtension().lastPathComponent.dropFirst("gobby-annotate-".count))
    // The ID is persisted and later sent to the extension, which validates it again on unstage.
    guard UUID(uuidString: exportId) != nil else { throw Failure.invalid("Invalid export ID") }
    try withLock { try updateDeleted { $0.insert(exportId) } }
    try withLock { try removeStaged(exportId) }
  }
  /// Export IDs the app deleted that the extension has not acknowledged yet.
  func deleted() throws -> [String] {
    try withLock { try readDeleted().sorted() }
  }
  /// Removes one staged ZIP; an already-missing ZIP counts as deleted. Caller holds the lock.
  private func removeStaged(_ exportId: String) throws {
    let staged = root.appendingPathComponent("gobby-annotate-\(exportId).zip")
    if FileManager.default.fileExists(atPath: staged.path) {
      try FileManager.default.removeItem(at: staged)
    }
  }
  /// Records live beside the ZIPs under a name that is not UUID-shaped, so `interruptedCount`
  /// never counts them as transfers.
  private var deletionRecord: URL { root.appendingPathComponent("deleted.json") }
  private func readDeleted() throws -> Set<String> {
    guard FileManager.default.fileExists(atPath: deletionRecord.path) else { return [] }
    return Set(try JSONDecoder().decode([String].self, from: Data(contentsOf: deletionRecord)))
  }
  /// Caller holds the lock.
  private func updateDeleted(_ change: (inout Set<String>) -> Void) throws {
    var ids = try readDeleted()
    change(&ids)
    if ids.isEmpty {
      if FileManager.default.fileExists(atPath: deletionRecord.path) {
        try FileManager.default.removeItem(at: deletionRecord)
      }
    } else {
      try writeReplacing(JSONEncoder().encode(ids.sorted()), to: deletionRecord)
    }
  }
  /// Replaces `url` with `data` through a uniquely named temp file in the same folder, renamed over
  /// the target. The replacement is atomic and nothing stages outside the store root; a failed
  /// write removes its temp file.
  private func writeReplacing(_ data: Data, to url: URL) throws {
    let temp = url.deletingLastPathComponent().appendingPathComponent(".\(UUID().uuidString).tmp")
    do {
      try data.write(to: temp, options: .withoutOverwriting)
      let file = try FileHandle(forUpdating: temp)
      defer { try? file.close() }
      try file.synchronize()
      guard rename(temp.path, url.path) == 0 else {
        throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
      }
    } catch {
      try? FileManager.default.removeItem(at: temp)
      throw error
    }
  }
  /// Runs `work` under the storage lock, creating the root first.
  private func withLock<T>(_ work: () throws -> T) throws -> T {
    try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
    let lock = open(root.appendingPathComponent(".lock").path, O_CREAT | O_RDWR | O_NOFOLLOW, 0o600)
    guard lock >= 0 else { throw Failure.invalid("Cannot lock export storage") }
    defer { close(lock) }
    guard flock(lock, LOCK_EX) == 0 else { throw Failure.invalid("Cannot lock export storage") }
    defer { flock(lock, LOCK_UN) }
    return try work()
  }
  /// Deletes every staged ZIP and leaves unfinished transfers alone.
  func deleteCompleted() throws {
    for file in try completed() { try delete(file) }
  }
  /// Counts unfinished transfers, which are the UUID-named folders `clearInterrupted` removes.
  func interruptedCount() throws -> Int {
    guard FileManager.default.fileExists(atPath: root.path) else { return 0 }
    return try FileManager.default.contentsOfDirectory(at: root, includingPropertiesForKeys: nil)
      .filter { UUID(uuidString: $0.lastPathComponent) != nil }.count
  }
  func clearInterrupted() throws {
    guard FileManager.default.fileExists(atPath: root.path) else { return }
    for entry in try FileManager.default.contentsOfDirectory(
      at: root, includingPropertiesForKeys: nil)
    {
      if UUID(uuidString: entry.lastPathComponent) != nil {
        try handle(
          ExportRequest(
            action: "cancel", transferId: entry.lastPathComponent, name: nil, size: nil,
            offset: nil, data: nil))
      }
    }
  }
}
