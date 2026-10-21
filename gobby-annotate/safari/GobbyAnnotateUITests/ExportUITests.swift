import CoreGraphics
import Foundation
import ImageIO
import XCTest

/// Captures the staged-export row at default and XXXL text sizes, in light and dark
/// appearance, for #23729.
///
/// Each capture checks its pixels before attaching them: a dark capture must render dark and a
/// light capture must render light, so a capture that renders the wrong appearance fails.
///
/// The row needs one staged ZIP in the App Group's Exports folder; stage one
/// before running, because the test runner cannot write to the app container.
final class ExportUITests: XCTestCase {
  override func setUpWithError() throws {
    continueAfterFailure = false
  }

  func testStagedRowAtDefaultTextSizeInLightAppearance() throws {
    try captureStagedRow(named: "export-row-default-light", textSize: nil, appearance: .light)
  }

  func testStagedRowAtDefaultTextSizeInDarkAppearance() throws {
    try captureStagedRow(named: "export-row-default-dark", textSize: nil, appearance: .dark)
  }

  func testStagedRowAtXXXLTextSizeInLightAppearance() throws {
    try captureStagedRow(
      named: "export-row-xxxl-light", textSize: "UICTContentSizeCategoryAccessibilityXXXL",
      appearance: .light)
  }

  func testStagedRowAtXXXLTextSizeInDarkAppearance() throws {
    try captureStagedRow(
      named: "export-row-xxxl-dark", textSize: "UICTContentSizeCategoryAccessibilityXXXL",
      appearance: .dark)
  }

  func testSwipeRevealsDeleteAndShareInEveryVariant() throws {
    for variant in Self.variants {
      let app = try launchExportList(variant)
      stagedRow(in: app).swipeLeft()
      XCTAssertTrue(
        app.buttons["Delete"].firstMatch.waitForExistence(timeout: 5),
        "\(variant.name): the trailing swipe has no Delete action")
      XCTAssertTrue(
        app.buttons["Share"].firstMatch.exists,
        "\(variant.name): the trailing swipe has no Share action")
      try app.performAccessibilityAudit(for: .contrast)
      capture(app, named: "export-swipe-\(variant.name)")
      app.terminate()
    }
  }

  func testEditModeShowsDoneInEveryVariant() throws {
    for variant in Self.variants {
      let app = try launchExportList(variant)
      app.buttons["Edit"].firstMatch.tap()
      XCTAssertTrue(
        app.buttons["Done"].firstMatch.waitForExistence(timeout: 5),
        "\(variant.name): Edit did not switch the list into edit mode")
      try app.performAccessibilityAudit(for: .contrast)
      capture(app, named: "export-edit-\(variant.name)")
      app.terminate()
    }
  }

  func testLongPressMenuShowsShareAndDeleteInEveryVariant() throws {
    for variant in Self.variants {
      let app = try launchExportList(variant)
      stagedRow(in: app).press(forDuration: 1.2)
      XCTAssertTrue(
        app.buttons["Share"].firstMatch.waitForExistence(timeout: 5),
        "\(variant.name): the long-press menu has no Share action")
      XCTAssertTrue(
        app.buttons["Delete"].firstMatch.exists,
        "\(variant.name): the long-press menu has no Delete action")
      try app.performAccessibilityAudit(for: .contrast)
      capture(app, named: "export-menu-\(variant.name)")
      app.terminate()
    }
  }

  /// One list state to capture: text size and appearance.
  private struct Variant {
    let name: String
    let textSize: String?
    let appearance: XCUIDevice.Appearance
  }

  private static let xxxl = "UICTContentSizeCategoryAccessibilityXXXL"
  private static let variants: [Variant] = [
    Variant(name: "default-light", textSize: nil, appearance: .light),
    Variant(name: "default-dark", textSize: nil, appearance: .dark),
    Variant(name: "xxxl-light", textSize: xxxl, appearance: .light),
    Variant(name: "xxxl-dark", textSize: xxxl, appearance: .dark),
  ]
  private static let stagedNamePredicate = NSPredicate(format: "label ENDSWITH %@", ".zip")

  /// Pins the app's color scheme at launch (see App.swift). The device appearance alone does not
  /// reach the first frame on the iOS 26.5 runtime, so the capture would otherwise render light.
  private static func colorSchemeArguments(_ appearance: XCUIDevice.Appearance) -> [String] {
    ["-GobbyAnnotateColorScheme", appearance == .dark ? "dark" : "light"]
  }

  /// Cold-launches the app under one variant and waits for a staged row. The device appearance
  /// is restored when the test ends.
  private func launchExportList(_ variant: Variant) throws -> XCUIApplication {
    let original = XCUIDevice.shared.appearance
    addTeardownBlock { XCUIDevice.shared.appearance = original }

    let app = XCUIApplication()
    app.terminate()
    XCUIDevice.shared.appearance = variant.appearance
    XCTAssertEqual(
      XCUIDevice.shared.appearance, variant.appearance,
      "\(variant.name): the device did not report the requested appearance before launch")
    app.launchArguments += Self.colorSchemeArguments(variant.appearance)
    if let textSize = variant.textSize {
      app.launchArguments += ["-UIPreferredContentSizeCategoryName", textSize]
    }
    app.launch()
    let list = app.tables.firstMatch.exists ? app.tables.firstMatch : app.collectionViews.firstMatch
    XCTAssertTrue(
      list.waitForExistence(timeout: 10), "\(variant.name): export list did not appear")
    // List rows below the viewport may not exist in the accessibility tree at XXXL.
    // Reveal them before the interaction tests query or activate them.
    _ = stagedRow(in: app)
    return app
  }

  /// The first staged-export row, scrolled into view.
  private func stagedRow(in app: XCUIApplication) -> XCUIElement {
    let row = app.staticTexts.matching(Self.stagedNamePredicate).firstMatch
    let list = app.tables.firstMatch.exists ? app.tables.firstMatch : app.collectionViews.firstMatch
    var swipes = 0
    while !row.isHittable && swipes < 12 {
      list.swipeUp()
      swipes += 1
    }
    XCTAssertTrue(
      row.isHittable, "No staged export row is visible; stage a ZIP before running this test")
    return row
  }

  /// Attaches the screenshot to the result bundle. When the script sets CAPTURE_OUT, also writes
  /// it there as a PNG.
  private func capture(_ app: XCUIApplication, named name: String) {
    let screenshot = app.screenshot()
    let attachment = XCTAttachment(screenshot: screenshot)
    attachment.name = name
    attachment.lifetime = .keepAlways
    add(attachment)
    guard let directory = ProcessInfo.processInfo.environment["CAPTURE_OUT"] else { return }
    do {
      try screenshot.pngRepresentation.write(
        to: URL(fileURLWithPath: directory).appendingPathComponent("\(name).png"))
    } catch {
      XCTFail("\(name): could not write the PNG to \(directory): \(error)")
    }
  }

  private func captureStagedRow(
    named name: String, textSize: String?, appearance: XCUIDevice.Appearance
  ) throws {
    let original = XCUIDevice.shared.appearance
    addTeardownBlock { XCUIDevice.shared.appearance = original }

    let app = XCUIApplication()
    // A process left running by an earlier case keeps the appearance it launched with. End it,
    // set the requested appearance, and confirm the device reports it, so the app cold-launches
    // under that appearance.
    app.terminate()
    XCUIDevice.shared.appearance = appearance
    XCTAssertEqual(
      XCUIDevice.shared.appearance, appearance,
      "\(name): the device did not report the requested appearance before launch")
    app.launchArguments += Self.colorSchemeArguments(appearance)
    if let textSize {
      app.launchArguments += ["-UIPreferredContentSizeCategoryName", textSize]
    }
    app.launch()

    let list = app.tables.firstMatch.exists ? app.tables.firstMatch : app.collectionViews.firstMatch
    XCTAssertTrue(list.waitForExistence(timeout: 10), "Export list did not appear")

    let row = app.staticTexts.matching(NSPredicate(format: "label ENDSWITH %@", ".zip")).firstMatch
    var swipes = 0
    while !row.isHittable && swipes < 12 {
      list.swipeUp()
      swipes += 1
    }
    XCTAssertTrue(
      row.isHittable, "No staged export row is visible; stage a ZIP before running this test")

    let screenshot = app.screenshot()
    let luminance = meanLuminance(of: screenshot)
    XCTAssertGreaterThanOrEqual(luminance, 0, "\(name): the captured screen could not be decoded")
    switch appearance {
    case .dark:
      XCTAssertLessThan(
        luminance, 0.4,
        "\(name) was captured in dark appearance but renders light (mean luminance \(luminance))")
    case .light:
      XCTAssertGreaterThan(
        luminance, 0.6,
        "\(name) was captured in light appearance but renders dark (mean luminance \(luminance))")
    default:
      break
    }

    let attachment = XCTAttachment(screenshot: screenshot)
    attachment.name = name
    attachment.lifetime = .keepAlways
    add(attachment)
  }

  /// Mean relative luminance of a screenshot from 0 (black) to 1 (white), or -1 when the
  /// PNG cannot be decoded.
  private func meanLuminance(of screenshot: XCUIScreenshot) -> Double {
    guard
      let source = CGImageSourceCreateWithData(screenshot.pngRepresentation as CFData, nil),
      let image = CGImageSourceCreateImageAtIndex(source, 0, nil)
    else { return -1 }
    let width = image.width
    let height = image.height
    let byteCount = width * height * 4
    let buffer = UnsafeMutableRawPointer.allocate(byteCount: byteCount, alignment: 16)
    defer { buffer.deallocate() }
    guard
      let context = CGContext(
        data: buffer, width: width, height: height, bitsPerComponent: 8,
        bytesPerRow: width * 4, space: CGColorSpaceCreateDeviceRGB(),
        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)
    else { return -1 }
    context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
    let pixels = buffer.bindMemory(to: UInt8.self, capacity: byteCount)
    var total = 0.0
    for offset in stride(from: 0, to: byteCount, by: 4) {
      let red = Double(pixels[offset])
      let green = Double(pixels[offset + 1])
      let blue = Double(pixels[offset + 2])
      total += 0.2126 * red + 0.7152 * green + 0.0722 * blue
    }
    return total / Double(width * height) / 255
  }
}
