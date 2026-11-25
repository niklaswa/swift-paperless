//
//  OfflineUITests.swift
//  swift-paperlessUITests
//
//  Drives the offline screens against a running mock server
//  (scripts/mock_paperless/server.py, see its README). Not part of CI, and
//  not one self-contained run: the tests are steps of a manual session, in
//  which the mock is stopped and started between them.
//

import XCTest

final class OfflineUITests: XCTestCase {
  private var mockURL: String {
    ProcessInfo.processInfo.environment["MOCK_URL"] ?? "http://127.0.0.1:8770"
  }

  override func setUpWithError() throws {
    continueAfterFailure = false
  }

  @MainActor
  private func attach(_ name: String) {
    let attachment = XCTAttachment(screenshot: XCUIScreen.main.screenshot())
    attachment.name = name
    attachment.lifetime = .keepAlways
    add(attachment)
  }

  /// Launches the app and, on a fresh install, logs in through the UI, so the
  /// server is a real one whose settings can be changed (the preview launch
  /// arguments give a server without an active connection). A no-op when the
  /// app is already logged in.
  @MainActor
  private func launchLoggedIn() throws -> XCUIApplication {
    let app = XCUIApplication()
    app.launch()
    let getStarted = app.buttons["Get started"]
    let url = app.textFields["paperless.example.com"]
    // The list's search field: there on iPhone and iPad alike, where the
    // title is a button on one and the navigation bar on the other.
    let documents = app.textFields["Search"]
    // The welcome page, the login form, or the document list.
    for _ in 0..<30 {
      if getStarted.exists || url.exists || documents.exists { break }
      sleep(1)
    }
    if getStarted.exists { getStarted.tap() }
    guard url.waitForExistence(timeout: 5) else { return app }

    url.tap()
    url.typeText(mockURL)
    let next = app.buttons["Continue"]
    XCTAssertTrue(next.waitForExistence(timeout: 20), app.debugDescription)
    for _ in 0..<20 where !next.isEnabled { sleep(1) }
    next.tap()

    let username = app.textFields["Username"]
    XCTAssertTrue(username.waitForExistence(timeout: 20), app.debugDescription)
    username.tap()
    username.typeText("admin")
    let password = app.secureTextFields["Password"]
    password.tap()
    password.typeText("mock-password")
    app.buttons["Log in"].tap()
    // The system's offer to save the password, where it comes up.
    let notNow = app.buttons["Not Now"]
    if notNow.waitForExistence(timeout: 8) { notNow.tap() }
    XCTAssertTrue(documents.waitForExistence(timeout: 60), app.debugDescription)
    return app
  }

  /// Focus the search field and empty it: the search text outlives a launch.
  @MainActor
  private func focusEmptySearchField(_ app: XCUIApplication) -> XCUIElement {
    let field = app.textFields["Search"]
    XCTAssertTrue(field.waitForExistence(timeout: 30))
    // The button next to the field that is only there while it holds text.
    let clear = app.buttons["xmark"]
    if clear.waitForExistence(timeout: 3) {
      clear.tap()
    }
    field.tap()
    return field
  }

  @MainActor
  private func openOfflineSync(_ app: XCUIApplication) throws {
    let menu = app.buttons["Menu of more options"]
    XCTAssertTrue(menu.waitForExistence(timeout: 30))
    menu.tap()
    let settings = app.buttons["Settings"]
    XCTAssertTrue(settings.waitForExistence(timeout: 5), app.debugDescription)
    settings.tap()
    let offline = app.staticTexts["Offline & Sync"]
    XCTAssertTrue(offline.waitForExistence(timeout: 10), app.debugDescription)
    offline.tap()
    XCTAssertTrue(app.staticTexts["Size of entire library"].waitForExistence(timeout: 10))
  }

  @MainActor
  func testLogin() throws {
    _ = try launchLoggedIn()
    sleep(3)
    attach("logged-in")
  }

  /// The Offline & Sync screen: the library size estimate and the storage
  /// figures.
  @MainActor
  func testOfflineSyncScreen() throws {
    let app = try launchLoggedIn()
    try openOfflineSync(app)
    // Let the sampled estimate land.
    sleep(6)
    attach("offline-sync-top")
    app.swipeUp()
    sleep(1)
    attach("offline-sync-middle")
    app.swipeUp()
    sleep(1)
    attach("offline-sync-bottom")
  }

  /// Turning on "Search all documents offline" starts downloading the texts.
  @MainActor
  func testEnableLibraryText() throws {
    let app = try launchLoggedIn()
    try openOfflineSync(app)
    sleep(5)
    // The switch's label carries its caption too.
    let toggle = app.switches.matching(
      NSPredicate(format: "label BEGINSWITH %@", "Search all documents offline")
    ).firstMatch
    XCTAssertTrue(toggle.waitForExistence(timeout: 5), app.debugDescription)
    if (toggle.value as? String) != "1" {
      // The switch itself, not the row's label.
      toggle.coordinate(withNormalizedOffset: CGVector(dx: 0.93, dy: 0.5)).tap()
    }
    XCTAssertEqual(toggle.value as? String, "1")
    sleep(8)
    attach("library-text-on")
    app.swipeUp()
    sleep(1)
    attach("library-text-status")
  }

  /// Typing a search while the server is unreachable answers from the device,
  /// and from the second word on without waiting for a request to fail.
  @MainActor
  func testSearchWhileUnreachable() throws {
    let app = try launchLoggedIn()
    let field = focusEmptySearchField(app)
    field.typeText("haftpflicht")
    XCTAssertTrue(app.staticTexts["Offline results"].waitForExistence(timeout: 90))
    attach("search-offline-first")
    field.typeText(" kündigung")
    sleep(2)
    XCTAssertTrue(app.staticTexts["Offline results"].exists)
    attach("search-offline-second")
    // A word no earlier run has searched for: an empty answer the server
    // once gave for the same search is cached, and shown as the server's.
    field.typeText(" nix\(Int(Date().timeIntervalSince1970))")
    XCTAssertTrue(app.staticTexts["No matches on this device"].waitForExistence(timeout: 10))
    attach("search-offline-empty")
  }

  /// Tell the mock how to answer: "hang" (never), "503", or "off" (normally).
  private func setMockFailure(_ mode: String) {
    var request = URLRequest(url: URL(string: "\(mockURL)/__mock/fail?mode=\(mode)")!)
    request.httpMethod = "POST"
    let done = expectation(description: "mock \(mode)")
    URLSession.shared.dataTask(with: request) { _, _, _ in done.fulfill() }.resume()
    wait(for: [done], timeout: 10)
  }

  /// A server that accepts connections and never answers. Its requests take
  /// minutes to fail, so the list doesn't wait for that: after a moment it
  /// shows what the device finds, and the server's answer replaces it once
  /// the server responds again.
  @MainActor
  func testHangingServer() throws {
    setMockFailure("off")
    let app = try launchLoggedIn()
    let field = focusEmptySearchField(app)
    sleep(5)
    setMockFailure("hang")
    defer { setMockFailure("off") }

    field.typeText("quittungsnummer-7781")
    let meanwhile = app.staticTexts["Results from this device"]
    let offline = app.staticTexts["Offline results"]
    let started = Date()
    for _ in 0..<40 where !meanwhile.exists && !offline.exists { usleep(250_000) }
    XCTAssertTrue(meanwhile.exists || offline.exists, app.debugDescription)
    print("FIELD-VALUE <\(field.value as? String ?? "?")>")
    print("LOCAL-RESULTS after \(Date().timeIntervalSince(started)) s")
    attach("hang-local-results")

    // The server answers again: the held requests are dropped, retried, and
    // the list becomes the server's.
    setMockFailure("off")
    for _ in 0..<180 where meanwhile.exists || offline.exists { sleep(1) }
    XCTAssertFalse(meanwhile.exists || offline.exists)
    attach("hang-server-answered")
  }

  /// With the server back, Retry replaces the device's answer with the
  /// server's.
  @MainActor
  func testRetryWhenReachableAgain() throws {
    let app = try launchLoggedIn()
    let field = app.textFields["Search"]
    XCTAssertTrue(field.waitForExistence(timeout: 30))
    let banner = app.staticTexts["Offline results"]
    if banner.waitForExistence(timeout: 5) {
      attach("before-retry")
      app.buttons["Retry"].firstMatch.tap()
    }
    for _ in 0..<30 where banner.exists { sleep(1) }
    XCTAssertFalse(banner.exists)
    attach("after-retry")
  }
}
