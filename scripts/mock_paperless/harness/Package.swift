// swift-tools-version: 6.0
// Decoding harness for scripts/mock_paperless/server.py: drives the app's own
// ApiRepository against a running mock and reports PASS/FAIL per step.
//
//   python3 scripts/mock_paperless/server.py --quiet &
//   cd scripts/mock_paperless/harness && swift run MockHarness

import PackageDescription

let package = Package(
  name: "MockHarness",
  platforms: [
    .macOS(.v14)
  ],
  dependencies: [
    .package(path: "../../../Networking"),
    .package(path: "../../../DataModel"),
    .package(path: "../../../Common"),
  ],
  targets: [
    .executableTarget(
      name: "MockHarness",
      dependencies: [
        .product(name: "Networking", package: "Networking"),
        .product(name: "DataModel", package: "DataModel"),
        .product(name: "Common", package: "Common"),
      ],
      swiftSettings: [
        .swiftLanguageMode(.v6)
      ]
    )
  ]
)
