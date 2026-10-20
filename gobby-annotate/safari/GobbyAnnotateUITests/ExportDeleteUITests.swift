import XCTest

/// Deletes one staged export in the app, relaunches, and captures that it stays gone while
/// another staged export remains (#23729 criterion 10). The host stages two ZIPs named
/// `gobby-annotate-<UUID>.zip` in the App Group `Exports` folder before running this test.
final class ExportDeleteUITests: XCTestCase {
  override func setUpWithError() throws {
    continueAfterFailure = false
  }

  func testDeletedExportStaysGoneAfterRelaunchInLightAppearance() throws {
    let original = XCUIDevice.shared.appearance
    addTeardownBlock { XCUIDevice.shared.appearance = original }
    XCUIDevice.shared.appearance = .light

    let app = XCUIApplication()
    app.launch()

    let rows = app.staticTexts.matching(NSPredicate(format: "label ENDSWITH %@", ".zip"))
    XCTAssertTrue(
      rows.firstMatch.waitForExistence(timeout: 10),
      "No staged export row; stage two ZIPs before running this test")
    let file = rows.firstMatch.label

    let list = app.tables.firstMatch.exists ? app.tables.firstMatch : app.collectionViews.firstMatch
    let row = app.staticTexts[file]
    var swipes = 0
    while !row.isHittable && swipes < 12 {
      list.swipeUp()
      swipes += 1
    }
    XCTAssertTrue(row.isHittable, "Export row for \(file) is not visible")
    row.swipeLeft()
    let delete = app.buttons["Delete"].firstMatch
    XCTAssertTrue(
      delete.waitForExistence(timeout: 5), "Delete swipe action for \(file) did not appear")
    delete.tap()

    let confirm = app.buttons["Delete export"]
    XCTAssertTrue(confirm.waitForExistence(timeout: 5), "Delete confirmation did not appear")
    confirm.tap()
    XCTAssertTrue(
      row.waitForNonExistence(timeout: 10),
      "Deleted export \(file) is still listed before relaunch")

    app.terminate()
    app.launch()
    XCTAssertTrue(
      rows.firstMatch.waitForExistence(timeout: 10),
      "Remaining staged export row is missing after relaunch")
    XCTAssertFalse(
      app.staticTexts[file].exists, "Deleted export \(file) returned after relaunch")

    let attachment = XCTAttachment(screenshot: app.screenshot())
    attachment.name = "export-delete-relaunch-default-light"
    attachment.lifetime = .keepAlways
    add(attachment)
  }
}
