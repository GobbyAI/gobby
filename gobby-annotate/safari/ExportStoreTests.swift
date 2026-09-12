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
}

@main
struct ExportStoreTestRunner {
  static func main() {
    let suite = ExportStoreTests.defaultTestSuite
    suite.run()
    exit(suite.testRun?.hasSucceeded == true ? 0 : 1)
  }
}
