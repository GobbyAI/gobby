import Foundation
import XCTest

final class ExportStoreTests: XCTestCase {
  @objc func testOrderedTransferPreservesBytesAndSurvivesReopening() throws {
    let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    defer { try? FileManager.default.removeItem(at: root) }
    let store = ExportStore(root: root)
    let id = UUID().uuidString
    let name = "gobby-annotate-\(UUID().uuidString).zip"
    let input = try XCTUnwrap(ProcessInfo.processInfo.environment["ANNOTATE_TEST_BUNDLE"])
    let bytes = try Data(contentsOf: URL(fileURLWithPath: input))
    XCTAssertGreaterThan(bytes.count, 192 * 1024)
    try store.handle(
      ExportRequest(
        action: "begin", transferId: id, name: name, size: bytes.count, offset: nil, data: nil))
    XCTAssertEqual(try store.completed(), [])
    for offset in stride(from: 0, to: bytes.count, by: 192 * 1024) {
      let chunk = bytes.subdata(in: offset..<min(offset + 192 * 1024, bytes.count))
      try ExportStore(root: root).handle(
        ExportRequest(
          action: "chunk", transferId: id, name: nil, size: nil, offset: offset,
          data: chunk.base64EncodedString()))
    }
    try store.handle(
      ExportRequest(action: "finish", transferId: id, name: nil, size: nil, offset: nil, data: nil))
    XCTAssertEqual(try store.completed().map(\.lastPathComponent), [name])
    XCTAssertEqual(try Data(contentsOf: root.appendingPathComponent(name)), bytes)
    let output = try XCTUnwrap(ProcessInfo.processInfo.environment["ANNOTATE_TEST_RESULT"])
    try Data(contentsOf: root.appendingPathComponent(name)).write(to: URL(fileURLWithPath: output))
  }

  @objc func testInterruptedAndOutOfOrderTransfersRemainHidden() throws {
    let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    defer { try? FileManager.default.removeItem(at: root) }
    let store = ExportStore(root: root)
    let id = UUID().uuidString
    try store.handle(
      ExportRequest(
        action: "begin", transferId: id, name: "gobby-annotate-\(UUID().uuidString).zip", size: 20,
        offset: nil, data: nil))
    XCTAssertThrowsError(
      try store.handle(
        ExportRequest(
          action: "chunk", transferId: id, name: nil, size: nil, offset: 1,
          data: Data([1]).base64EncodedString())))
    XCTAssertThrowsError(
      try store.handle(
        ExportRequest(
          action: "finish", transferId: id, name: nil, size: nil, offset: nil, data: nil)))
    XCTAssertEqual(try ExportStore(root: root).completed(), [])
    try store.clearInterrupted()
    XCTAssertFalse(FileManager.default.fileExists(atPath: root.appendingPathComponent(id).path))
  }

  @objc func testUnsafeAndOversizedTransfersAreRejected() throws {
    let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    defer { try? FileManager.default.removeItem(at: root) }
    let store = ExportStore(root: root)
    XCTAssertThrowsError(
      try store.handle(
        ExportRequest(
          action: "begin", transferId: "../escape", name: "x.zip", size: 20, offset: nil, data: nil)
      ))
    XCTAssertThrowsError(
      try store.handle(
        ExportRequest(
          action: "begin", transferId: UUID().uuidString, name: "../x.zip", size: 20, offset: nil,
          data: nil)))
    XCTAssertThrowsError(
      try store.handle(
        ExportRequest(
          action: "begin", transferId: UUID().uuidString,
          name: "gobby-annotate-\(UUID().uuidString).zip", size: ExportStore.maximumBytes + 1,
          offset: nil, data: nil)))
    XCTAssertEqual(try store.completed(), [])
  }

  @objc func testDeleteRemovesOnlyTheNamedExportAndKeepsUnfinishedTransfers() throws {
    let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    defer { try? FileManager.default.removeItem(at: root) }
    let store = ExportStore(root: root)
    let kept = UUID().uuidString
    let removed = UUID().uuidString
    try stage(store, exportId: kept)
    try stage(store, exportId: removed)
    let partial = UUID().uuidString
    try store.handle(
      ExportRequest(
        action: "begin", transferId: partial, name: "gobby-annotate-\(UUID().uuidString).zip",
        size: 20, offset: nil, data: nil))
    try store.delete(root.appendingPathComponent("gobby-annotate-\(removed).zip"))
    XCTAssertEqual(
      try store.completed().map(\.lastPathComponent), ["gobby-annotate-\(kept).zip"])
    XCTAssertTrue(FileManager.default.fileExists(atPath: root.appendingPathComponent(partial).path))
    // The extension can repeat a delete for an export the app already removed.
    try store.handle(
      ExportRequest(
        action: "delete", transferId: removed, name: nil, size: nil, offset: nil, data: nil))
    XCTAssertEqual(
      try store.completed().map(\.lastPathComponent), ["gobby-annotate-\(kept).zip"])
    try store.deleteCompleted()
    XCTAssertEqual(try store.completed(), [])
    XCTAssertTrue(FileManager.default.fileExists(atPath: root.appendingPathComponent(partial).path))
  }

  @objc func testDeleteRejectsIdsThatAreNotExportIds() throws {
    let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    defer { try? FileManager.default.removeItem(at: root) }
    let store = ExportStore(root: root)
    let exportId = UUID().uuidString
    try stage(store, exportId: exportId)
    XCTAssertThrowsError(
      try store.handle(
        ExportRequest(
          action: "delete", transferId: "../\(exportId)", name: nil, size: nil, offset: nil,
          data: nil)))
    XCTAssertEqual(
      try store.completed().map(\.lastPathComponent), ["gobby-annotate-\(exportId).zip"])
  }

  private func stage(_ store: ExportStore, exportId: String) throws {
    let bytes = Data([0x50, 0x4b, 0x03, 0x04, 0x14, 0x00])
    let transferId = UUID().uuidString
    try store.handle(
      ExportRequest(
        action: "begin", transferId: transferId, name: "gobby-annotate-\(exportId).zip",
        size: bytes.count, offset: nil, data: nil))
    try store.handle(
      ExportRequest(
        action: "chunk", transferId: transferId, name: nil, size: nil, offset: 0,
        data: bytes.base64EncodedString()))
    try store.handle(
      ExportRequest(
        action: "finish", transferId: transferId, name: nil, size: nil, offset: nil, data: nil))
  }
}

@main
struct ExportStoreTestRunner {
  static func main() {
    let suite = ExportStoreTests.defaultTestSuite
    suite.run()
    exit(suite.testRun?.hasSucceeded == true ? 0 : 1)
  }
}
