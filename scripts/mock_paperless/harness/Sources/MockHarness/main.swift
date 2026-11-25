//
//  MockHarness
//
//  Drives the app's own networking layer (ApiRepository) against a running
//  scripts/mock_paperless/server.py and prints PASS/FAIL per step. Every
//  response therefore goes through the real wire types in
//  Networking/Sources/Networking/Api/Wire.
//
//  Usage: swift run MockHarness [--url http://127.0.0.1:8765] [--token mock-token]
//  The mock has to be started with at least 20000 documents (the default is
//  25000) so that all needle documents exist. The run mutates the mock's
//  in-memory state (it creates and deletes a few objects); restart the mock
//  for a pristine library.
//

import Common
import DataModel
import Foundation
import Networking

// MARK: - Support

/// A `FileIndex` in a dictionary, like the one in NetworkingTests.
actor MemoryFileIndex: FileIndex {
  struct Entry {
    var modified: Date?
    var checksum: String?
  }

  private var entries: [ContentStore.Key: Entry] = [:]

  func isFresh(_ key: ContentStore.Key, modified: Date) async throws -> Bool {
    entries[key]?.modified == modified
  }

  func storedChecksum(_ key: ContentStore.Key) async throws -> String? {
    entries[key]?.checksum
  }

  func recordStore(
    _ key: ContentStore.Key, documentID: UInt, size: Int64, modified: Date?, checksum: String?,
    storedAt: Date
  ) async throws {
    entries[key] = Entry(modified: modified, checksum: checksum)
  }

  func recordAccess(_ key: ContentStore.Key, at date: Date) async throws {}

  func forget(_ key: ContentStore.Key) async throws {
    entries[key] = nil
  }

  func checksum(_ key: ContentStore.Key) -> String? {
    entries[key]?.checksum
  }
}

struct CheckFailed: Error, CustomStringConvertible {
  let description: String
}

func check(_ condition: @autoclosure () -> Bool, _ message: @autoclosure () -> String) throws {
  if !condition() { throw CheckFailed(description: message()) }
}

func argument(_ name: String, default value: String) -> String {
  let args = CommandLine.arguments
  if let index = args.firstIndex(of: name), index + 1 < args.count {
    return args[index + 1]
  }
  return value
}

@MainActor
final class Harness {
  let base: URL
  let token: String
  var passed = 0
  var failed = 0

  init(base: URL, token: String) {
    self.base = base
    self.token = token
  }

  func step(_ name: String, _ body: @MainActor () async throws -> String) async {
    let start = ContinuousClock.now
    do {
      let detail = try await body()
      let elapsed = ContinuousClock.now - start
      let ms = Int(elapsed.components.seconds * 1000)
        + Int(elapsed.components.attoseconds / 1_000_000_000_000_000)
      passed += 1
      print("PASS  \(name): \(detail)  [\(ms) ms]")
    } catch {
      failed += 1
      print("FAIL  \(name): \(error)")
    }
  }

  /// Raw request outside the repository (login probe, control endpoints).
  func raw(
    _ method: String, _ path: String, headers: [String: String] = [:], body: Data? = nil,
    authenticated: Bool = true
  ) async throws -> (Data, HTTPURLResponse) {
    guard let url = URL(string: path, relativeTo: base) else {
      throw CheckFailed(description: "bad path \(path)")
    }
    var request = URLRequest(url: url)
    request.httpMethod = method
    request.httpBody = body
    request.cachePolicy = .reloadIgnoringLocalCacheData
    if authenticated {
      request.setValue("Token \(token)", forHTTPHeaderField: "Authorization")
    }
    for (key, value) in headers {
      request.setValue(value, forHTTPHeaderField: key)
    }
    let (data, response) = try await URLSession.shared.data(for: request)
    guard let http = response as? HTTPURLResponse else {
      throw CheckFailed(description: "not an HTTP response")
    }
    return (data, http)
  }

  func json(_ method: String, _ path: String) async throws -> [String: Any] {
    let (data, response) = try await raw(method, path)
    try check(response.statusCode == 200, "\(method) \(path) -> \(response.statusCode)")
    return try JSONSerialization.jsonObject(with: data) as? [String: Any] ?? [:]
  }
}

// MARK: - Run

@MainActor
func run() async -> Int32 {
  let urlString = argument(
    "--url", default: ProcessInfo.processInfo.environment["MOCK_URL"] ?? "http://127.0.0.1:8765")
  let token = argument(
    "--token", default: ProcessInfo.processInfo.environment["MOCK_TOKEN"] ?? "mock-token")
  guard let base = URL(string: urlString) else {
    print("FAIL  invalid --url \(urlString)")
    return 2
  }
  let h = Harness(base: base, token: token)
  print("Harness against \(base.absoluteString)")

  // -- Login flow, as LoginViewModel does it -------------------------------

  await h.step("login probe (GET /api/token/ per API version)") {
    var accepted: UInt?
    var rejected: [UInt] = []
    for version in stride(
      from: Int(ApiRepository.maximumApiVersion) + 1,
      through: Int(ApiRepository.minimumApiVersion), by: -1)
    {
      let (_, response) = try await h.raw(
        "GET", "/api/token/", headers: ["Accept": "application/json; version=\(version)"],
        authenticated: false)
      if response.statusCode == 405 {
        accepted = UInt(version)
        break
      }
      try check(response.statusCode == 406, "unexpected status \(response.statusCode)")
      rejected.append(UInt(version))
    }
    let version = try accepted ?? { throw CheckFailed(description: "no version accepted") }()
    return "accepted version \(version), 406 for \(rejected)"
  }

  await h.step("token login (POST /api/token/)") {
    let body = try JSONSerialization.data(withJSONObject: ["username": "admin", "password": "x"])
    let (data, response) = try await h.raw(
      "POST", "/api/token/", headers: ["Content-Type": "application/json"], body: body,
      authenticated: false)
    try check(response.statusCode == 200, "status \(response.statusCode)")
    let decoded = try JSONSerialization.jsonObject(with: data) as? [String: String]
    try check(decoded?["token"] == token, "token mismatch: \(decoded ?? [:])")
    return "token returned"
  }

  await h.step("request without token is rejected") {
    let anonymous = await ApiRepository(
      connection: Connection(url: base, token: nil, identityName: nil), mode: .debug,
      contentStore: nil, fileIndex: nil)
    do {
      _ = try await anonymous.currentUser()
    } catch RequestError.unauthorized {
      return "RequestError.unauthorized, versions known: \(anonymous.versions != nil)"
    }
    throw CheckFailed(description: "expected RequestError.unauthorized")
  }

  // -- Repository -----------------------------------------------------------

  let serverID = UUID()
  let connection = Connection(url: base, token: token, identityName: nil, serverID: serverID)
  let repo = await ApiRepository(
    connection: connection, mode: .debug, contentStore: nil, fileIndex: nil)

  await h.step("API version probe") {
    let versions = try repo.versions ?? { throw CheckFailed(description: "no versions") }()
    try check(repo.supports(feature: .documentIDFilter), "documentIDFilter not supported")
    return
      "api \(versions.apiVersion), backend \(versions.backendVersion), effective api \(repo.effectiveApiVersion), searchApi \(repo.searchApi), taskListEnvelope \(repo.supports(feature: .taskListEnvelope)), hasDuplicatesFilter \(repo.supports(feature: .hasDuplicatesFilter))"
  }

  await h.step("remoteVersion") {
    let remote = try await repo.remoteVersion()
    try check(remote.version != nil, "version did not parse")
    return "version \(remote.version!), updateAvailable \(remote.updateAvailable)"
  }

  var me: User?
  await h.step("uiSettings / currentUser") {
    let settings = try await repo.uiSettings()
    let user = try await repo.currentUser()
    me = user
    try check(settings.user.id == user.id, "user mismatch")
    try check(settings.permissions.test(.view, for: .document), "no view_document permission")
    return
      "user \(user.username) (#\(user.id), superuser \(user.isSuperUser)), appTitle \(settings.settings.appTitle ?? "nil"), sidebar views \(settings.settings.savedViews.sidebarViewsVisibleIds)"
  }

  var tags: [Tag] = []
  var correspondents: [Correspondent] = []
  var documentTypes: [DocumentType] = []
  var savedViews: [SavedView] = []

  await h.step("tags") {
    tags = try await repo.tags()
    let nested = tags.filter { $0.parent != nil }.count
    try check(tags.count >= 60, "only \(tags.count) tags")
    try check(tags.contains { $0.isInboxTag }, "no inbox tag")
    // Tag nesting arrived with paperless-ngx 2.19.
    if let backend = repo.versions?.backendVersion, backend >= Version(2, 19, 0) {
      try check(nested > 0, "no nested tags")
    }
    return "\(tags.count) tags, \(nested) with a parent, inbox tag #\(tags.first { $0.isInboxTag }!.id)"
  }
  await h.step("correspondents") {
    correspondents = try await repo.correspondents()
    try check(correspondents.count >= 200, "only \(correspondents.count)")
    let counted = correspondents.compactMap(\.documentCount).reduce(0, +)
    return "\(correspondents.count) correspondents, \(counted) documents assigned"
  }
  await h.step("documentTypes") {
    documentTypes = try await repo.documentTypes()
    try check(documentTypes.count >= 25, "only \(documentTypes.count)")
    return "\(documentTypes.count) document types"
  }
  await h.step("storagePaths") {
    let paths = try await repo.storagePaths()
    try check(paths.count >= 10, "only \(paths.count)")
    return "\(paths.count) storage paths, e.g. \(paths[0].path)"
  }
  await h.step("savedViews") {
    savedViews = try await repo.savedViews()
    try check(savedViews.count >= 6, "only \(savedViews.count)")
    let rules = savedViews.map(\.filterRules.count).reduce(0, +)
    return "\(savedViews.count) saved views with \(rules) rules: \(savedViews.map(\.name))"
  }
  await h.step("customFields") {
    let fields = try await repo.customFields()
    try check(fields.count >= 5, "only \(fields.count)")
    return "\(fields.count) custom fields: \(fields.map { "\($0.name):\($0.dataType.rawValue)" })"
  }
  await h.step("users / groups") {
    let users = try await repo.users()
    let groups = try await repo.groups()
    try check(users.count >= 2 && groups.count >= 1, "users \(users.count), groups \(groups.count)")
    return "\(users.count) users \(users.map(\.username)), \(groups.count) groups \(groups.map(\.name))"
  }
  await h.step("serverConfiguration") {
    let config = try await repo.serverConfiguration()
    return "config #\(config.id), ASN prefix \(config.barcodeAsnPrefix ?? "nil")"
  }
  await h.step("statistics (raw)") {
    let stats = try await h.json("GET", "/api/statistics/")
    let total = stats["documents_total"] as? Int ?? -1
    let characters = stats["character_count"] as? Int ?? -1
    try check(total > 0 && characters > 0, "\(stats)")
    return
      "documents_total \(total), documents_inbox \(stats["documents_inbox"] ?? "nil"), character_count \(characters), current_asn \(stats["current_asn"] ?? "nil")"
  }

  // -- Document lists -------------------------------------------------------

  var defaultFilter = FilterState.empty
  defaultFilter.sortField = .created
  defaultFilter.sortOrder = .descending

  var firstPage: [Document] = []
  var total: UInt = 0
  await h.step("documents(filter: default) - 3 pages of 250") {
    let source = try repo.documents(filter: defaultFilter)
    var all: [Document] = []
    for _ in 0..<3 {
      all += try await source.fetch(limit: Endpoint.defaultDocumentPageSize)
    }
    total = await source.totalCount ?? 0
    firstPage = all
    try check(all.count == 750, "got \(all.count) documents")
    try check(Set(all.map(\.id)).count == 750, "duplicate ids across pages")
    try check(zip(all, all.dropFirst()).allSatisfy { $0.created >= $1.created }, "not sorted")
    try check(all.allSatisfy { $0.permissions != nil }, "full_perms missing permissions")
    // Document versions arrived with paperless-ngx 3.0.
    if let backend = repo.versions?.backendVersion, backend >= Version(3, 0, 0) {
      try check(all.allSatisfy { !$0.versions.isEmpty }, "no versions")
    }
    let notes = all.filter { $0.notes.count > 0 }.count
    let fields = all.filter { !$0.customFields.isEmpty }.count
    return
      "750 decoded of \(total); \(notes) with notes, \(fields) with custom fields, \(all.filter { $0.asn != nil }.count) with ASN"
  }

  func count(_ filter: FilterState) async throws -> (UInt, [Document]) {
    let source = try repo.documents(filter: filter)
    let page = try await source.fetch(limit: 50)
    return (await source.totalCount ?? 0, page)
  }

  await h.step("title search (\(repo.searchApi))") {
    let (titleHits, page) = try await count(
      defaultFilter.with {
        $0.searchText = "Haftpflichtversicherung"
        $0.searchMode = .title
      })
    try check(titleHits > 0, "no hits")
    try check(
      page.allSatisfy { $0.title.localizedCaseInsensitiveContains("Haftpflichtversicherung") },
      "a hit without the word in its title")
    return "\(titleHits) documents with 'Haftpflichtversicherung' in the title"
  }
  await h.step("title+content search finds more than title search") {
    let (titleHits, _) = try await count(
      defaultFilter.with {
        $0.searchText = "Haftpflichtversicherung"
        $0.searchMode = .title
      })
    let (bothHits, _) = try await count(
      defaultFilter.with {
        $0.searchText = "Haftpflichtversicherung"
        $0.searchMode = .titleContent
      })
    try check(bothHits > titleHits, "title+content \(bothHits) <= title \(titleHits)")
    return "title \(titleHits) < title+content \(bothHits)"
  }
  await h.step("needle 'zebrafink' (content only, exactly 3)") {
    let (hits, page) = try await count(
      defaultFilter.with {
        $0.searchText = "zebrafink"
        $0.searchMode = .titleContent
      })
    let (titleHits, _) = try await count(
      defaultFilter.with {
        $0.searchText = "zebrafink"
        $0.searchMode = .title
      })
    try check(hits == 3, "expected 3, got \(hits)")
    try check(Set(page.map(\.id)) == [137, 4242, 17011], "ids \(page.map(\.id))")
    try check(titleHits == 0, "title search found \(titleHits)")
    return "ids \(page.map(\.id).sorted()), 0 by title"
  }
  await h.step("needle 'quittungsnummer-7781' (exactly 1), advanced query") {
    let (hits, page) = try await count(
      defaultFilter.with {
        $0.searchText = "quittungsnummer-7781"
        $0.searchMode = .titleContent
      })
    let (queryHits, _) = try await count(
      defaultFilter.with {
        $0.searchText = "quittungsnummer-7781"
        $0.searchMode = .advanced
      })
    try check(hits == 1 && page.first?.id == 2718, "hits \(hits), ids \(page.map(\.id))")
    try check(queryHits == 1, "advanced query found \(queryHits)")
    return "id \(page[0].id); advanced query agrees"
  }
  await h.step("needle 'Xylophonwartung' (title only, exactly 2)") {
    let (hits, page) = try await count(
      defaultFilter.with {
        $0.searchText = "xylophonwartung"
        $0.searchMode = .title
      })
    try check(hits == 2 && Set(page.map(\.id)) == [314, 9001], "ids \(page.map(\.id))")
    return "ids \(page.map(\.id).sorted())"
  }
  await h.step("tag filter (inbox, allOf)") {
    let inbox = try tags.first { $0.isInboxTag } ?? { throw CheckFailed(description: "no inbox") }()
    let (hits, page) = try await count(
      defaultFilter.with { $0.tags = .allOf(include: [inbox.id], exclude: []) })
    try check(hits > 0 && page.allSatisfy { $0.tags.contains(inbox.id) }, "bad tag filter")
    let (none, untagged) = try await count(defaultFilter.with { $0.tags = .notAssigned })
    try check(untagged.allSatisfy(\.tags.isEmpty), "notAssigned returned tagged documents")
    let (anyOf, _) = try await count(
      defaultFilter.with { $0.tags = .anyOf(ids: Array(tags.prefix(3).map(\.id))) })
    return "\(hits) in inbox, \(none) untagged, \(anyOf) with any of the first 3 tags"
  }
  await h.step("correspondent / document type / owner filters") {
    let correspondent = correspondents[0]
    let (hits, page) = try await count(
      defaultFilter.with { $0.correspondent = .anyOf(ids: [correspondent.id]) })
    try check(page.allSatisfy { $0.correspondent == correspondent.id }, "wrong correspondent")
    try check(UInt(hits) == correspondent.documentCount ?? 0, "count != document_count")
    let (notAssigned, unassigned) = try await count(
      defaultFilter.with { $0.correspondent = .notAssigned })
    try check(unassigned.allSatisfy { $0.correspondent == nil }, "notAssigned wrong")
    let (noneOf, _) = try await count(
      defaultFilter.with { $0.correspondent = .noneOf(ids: [correspondent.id]) })
    try check(noneOf + hits == total, "noneOf + anyOf != total")
    let (typed, typePage) = try await count(
      defaultFilter.with { $0.documentType = .anyOf(ids: [documentTypes[0].id]) })
    try check(typePage.allSatisfy { $0.documentType == documentTypes[0].id }, "wrong type")
    let (owned, _) = try await count(defaultFilter.with { $0.owner = .anyOf(ids: [me?.id ?? 1]) })
    let (unowned, _) = try await count(defaultFilter.with { $0.owner = .notAssigned })
    return
      "\(correspondent.name): \(hits), none: \(notAssigned), type \(documentTypes[0].name): \(typed), owned by me: \(owned), no owner: \(unowned)"
  }
  await h.step("ASN and date filters") {
    let (withAsn, _) = try await count(defaultFilter.with { $0.asn = .isNotNull })
    let (withoutAsn, _) = try await count(defaultFilter.with { $0.asn = .isNull })
    try check(withAsn + withoutAsn == total, "ASN null/not null do not add up")
    let (greater, page) = try await count(defaultFilter.with { $0.asn = .greaterThan(100) })
    try check(page.allSatisfy { ($0.asn ?? 0) > 100 }, "asn__gt wrong")
    let (less, _) = try await count(defaultFilter.with { $0.asn = .lessThan(100) })
    let (equal, _) = try await count(defaultFilter.with { $0.asn = .equalTo(100) })
    try check(less == 99 && equal == 1, "asn lt \(less), eq \(equal)")
    let start = Calendar.current.date(from: DateComponents(year: 2024, month: 1, day: 1))!
    let end = Calendar.current.date(from: DateComponents(year: 2024, month: 12, day: 31))!
    let (in2024, datePage) = try await count(
      defaultFilter.with { $0.date.created = .between(start: start, end: end) })
    try check(
      datePage.allSatisfy { Calendar.current.component(.year, from: $0.created) == 2024 },
      "created range wrong")
    let (thisYear, _) = try await count(
      defaultFilter.with { $0.date.created = .range(.currentYear) })
    let (addedRecently, _) = try await count(
      defaultFilter.with { $0.date.added = .range(.within(num: -3, interval: .month)) })
    return
      "ASN: \(withAsn) with, \(withoutAsn) without, \(greater) > 100; created in 2024: \(in2024), this year: \(thisYear), added within 3 months: \(addedRecently)"
  }
  await h.step("ordering by every sort field, both directions") {
    var checked: [String] = []
    for field in SortField.allCases + [.customField(1)] {
      for order in [DataModel.SortOrder.ascending, .descending] {
        var filter = FilterState.empty
        filter.sortField = field
        filter.sortOrder = order
        let source = try repo.documents(filter: filter)
        let page = try await source.fetch(limit: 300)
        try check(page.count == 300, "\(field.rawValue) \(order): \(page.count) documents")
        try check(Set(page.map(\.id)).count == 300, "\(field.rawValue): duplicate ids")
        let ordered: Bool
        let ascending = order == .ascending
        switch field {
        case .created:
          ordered = zip(page, page.dropFirst()).allSatisfy {
            ascending ? $0.created <= $1.created : $0.created >= $1.created
          }
        case .title:
          ordered = zip(page, page.dropFirst()).allSatisfy {
            ascending
              ? $0.title.lowercased() <= $1.title.lowercased()
              : $0.title.lowercased() >= $1.title.lowercased()
          }
        case .asn:
          // nulls sort last ascending and first descending, as PostgreSQL does
          ordered = zip(page, page.dropFirst()).allSatisfy {
            let a = $0.asn ?? UInt.max
            let b = $1.asn ?? UInt.max
            return ascending ? a <= b : a >= b
          }
        case .added:
          ordered = zip(page, page.dropFirst()).allSatisfy {
            ascending ? $0.added! <= $1.added! : $0.added! >= $1.added!
          }
        case .modified:
          ordered = zip(page, page.dropFirst()).allSatisfy {
            ascending ? $0.modified! <= $1.modified! : $0.modified! >= $1.modified!
          }
        case .pageCount:
          ordered = zip(page, page.dropFirst()).allSatisfy {
            ascending ? $0.pageCount! <= $1.pageCount! : $0.pageCount! >= $1.pageCount!
          }
        default:
          ordered = true
        }
        try check(ordered, "\(field.rawValue) \(order) is not sorted")
      }
      checked.append(field.rawValue)
    }
    return checked.joined(separator: ", ")
  }

  var allIDs: [UInt] = []
  await h.step("documentIDs(filter: .empty)") {
    allIDs = try await repo.documentIDs(filter: .empty)
    try check(UInt(allIDs.count) == total, "\(allIDs.count) ids, list count \(total)")
    try check(allIDs == allIDs.sorted(), "not in id order")
    try check(Set(allIDs).count == allIDs.count, "duplicates")
    return "\(allIDs.count) ids, \(allIDs.first ?? 0)...\(allIDs.last ?? 0)"
  }
  await h.step("orderedDocumentIDs(filter:) keeps the query order") {
    let ordered = try await repo.orderedDocumentIDs(filter: defaultFilter)
    try check(ordered.count == allIDs.count, "\(ordered.count) ids")
    try check(Array(ordered.prefix(750)) == firstPage.map(\.id), "order differs from the list")
    return "\(ordered.count) ids, first 750 match the paged list"
  }
  await h.step("documents(ids:)") {
    let ids = Array(allIDs.prefix(120)) + [999_999_999]
    let documents = try await repo.documents(ids: ids)
    try check(documents.count == 120, "got \(documents.count)")
    try check(Set(documents.map(\.id)) == Set(ids.dropLast()), "wrong ids")
    return "120 of 121 requested ids exist"
  }
  await h.step("fetchContents(ids:) without a content store") {
    let ids = Array(allIDs.prefix(200))
    let fetched = try await repo.fetchContents(ids: ids)
    try check(fetched == Set(ids), "texts for \(fetched.count) of \(ids.count)")
    return "\(fetched.count) texts"
  }

  // -- Content store: texts, files, ETag ------------------------------------

  let storeRoot = FileManager.default.temporaryDirectory
    .appendingPathComponent("MockHarness-\(UUID().uuidString)")
  try? FileManager.default.createDirectory(at: storeRoot, withIntermediateDirectories: true)
  defer { try? FileManager.default.removeItem(at: storeRoot) }
  let index = MemoryFileIndex()
  var storing: ApiRepository?
  if let store = try? ContentStore(root: storeRoot) {
    storing = await ApiRepository(
      connection: connection, mode: .debug, contentStore: store, fileIndex: index)
  }

  await h.step("fetchContents(ids:) into a content store, storedText") {
    let storing = try storing ?? { throw CheckFailed(description: "no content store") }()
    let fetched = try await storing.fetchContents(ids: [137, 138, 139, 2718])
    try check(fetched.count == 4, "texts for \(fetched.count)")
    let document = try await storing.document(id: 137) ?? { throw CheckFailed(description: "137") }()
    let text = try await storing.storedText(document: document)
      ?? { throw CheckFailed(description: "no stored text") }()
    try check(text.contains("zebrafink"), "stored text lacks the needle")
    return "stored text of #137 has \(text.count) characters and contains 'zebrafink'"
  }

  var sample: Document?
  await h.step("document(id:)") {
    let document = try await repo.document(id: 2718)
      ?? { throw CheckFailed(description: "nil") }()
    sample = document
    try check(document.id == 2718, "wrong id")
    let missing = try await repo.document(id: 999_999_999)
    try check(missing == nil, "missing document was not nil")
    return
      "'\(document.title)' created \(document.created.formatted(.iso8601.year().month().day())), \(document.pageCount ?? 0) pages, mime \(document.mimeType ?? "nil"), versions \(document.versions.map(\.id))"
  }
  await h.step("document(asn:)") {
    let document = try await repo.document(asn: 100)
      ?? { throw CheckFailed(description: "nil") }()
    try check(document.asn == 100, "asn \(String(describing: document.asn))")
    let next = try await repo.nextAsn()
    try check(next > 100, "next ASN \(next)")
    return "#\(document.id) has ASN 100, next ASN \(next)"
  }

  var metadata: Metadata?
  await h.step("metadata") {
    let data = try await repo.metadata(documentId: 2718)
    metadata = data
    try check(data.originalSize > 0 && !data.originalChecksum.isEmpty, "empty metadata")
    return
      "original \(data.originalSize) B \(data.originalMimeType) \(data.originalChecksum.prefix(12))..., archive \(data.archiveSize ?? 0) B, lang \(data.lang), \(data.originalMetadata.count) metadata items"
  }
  await h.step("notes / createNote / deleteNote") {
    let withNotes = try firstPage.first { $0.notes.count > 0 }
      ?? { throw CheckFailed(description: "no document with notes on the first pages") }()
    let notes = try await repo.notes(documentId: withNotes.id)
    try check(notes.count == withNotes.notes.count, "\(notes.count) != \(withNotes.notes.count)")
    let created = try await repo.createNote(
      documentId: withNotes.id, note: ProtoDocument.Note(note: "harness note"))
    try check(created.count == notes.count + 1, "note was not added")
    let added = try created.first { $0.note == "harness note" }
      ?? { throw CheckFailed(description: "created note missing") }()
    let after = try await repo.deleteNote(id: added.id, documentId: withNotes.id)
    try check(after.count == notes.count, "note was not deleted")
    return "#\(withNotes.id): \(notes.count) notes, +1, -1; author \(notes[0].user?.username ?? "nil")"
  }
  await h.step("suggestions") {
    let suggestions = try await repo.suggestions(documentId: 2718)
    return
      "correspondents \(suggestions.correspondents), tags \(suggestions.tags), types \(suggestions.documentTypes), paths \(suggestions.storagePaths), \(suggestions.dates.count) dates"
  }
  await h.step("thumbnailData") {
    let document = try sample ?? { throw CheckFailed(description: "no sample") }()
    let data = try await repo.thumbnailData(document: document)
    try check(data.starts(with: [0x89, 0x50, 0x4E, 0x47]), "not a PNG")
    let image = try await repo.thumbnail(document: document)
    try check(image != nil, "thumbnail did not decode as an image")
    return "\(data.count) B PNG, decodes as an image"
  }
  await h.step("download (archive + original, temporary file)") {
    let document = try sample ?? { throw CheckFailed(description: "no sample") }()
    let meta = try metadata ?? { throw CheckFailed(description: "no metadata") }()
    // Both land at the same temporary path (same suggested filename), so read
    // the archive before fetching the original.
    let archive = try await repo.download(document: document, original: false)
    let archiveData = try Data(contentsOf: archive)
    let original = try await repo.download(document: document, original: true)
    let originalData = try Data(contentsOf: original)
    try check(archiveData.starts(with: Array("%PDF-".utf8)), "archive is not a PDF")
    try check(Int64(archiveData.count) == meta.archiveSize, "archive size != metadata")
    try check(Int64(originalData.count) == meta.originalSize, "original size != metadata")
    return
      "archive \(archiveData.count) B as '\(archive.lastPathComponent)', original \(originalData.count) B; sizes match metadata"
  }
  await h.step("download into the content store, ETag revalidation (304)") {
    let storing = try storing ?? { throw CheckFailed(description: "no content store") }()
    let meta = try metadata ?? { throw CheckFailed(description: "no metadata") }()
    var document = try await storing.document(id: 2718)
      ?? { throw CheckFailed(description: "nil") }()
    let first = try await storing.download(document: document, original: false)
    let key = ContentStore.Key(
      serverID: serverID, versionID: document.currentVersionID, kind: .archive)
    let checksum = await index.checksum(key)
    try check(checksum == meta.archiveChecksum, "stored ETag \(checksum ?? "nil") != archive checksum")
    let before = try await h.json("GET", "/__mock/stats")["not_modified"] as? Int ?? 0
    // A metadata edit bumps `modified`; the file is unchanged, so the server answers 304.
    _ = try await h.raw("POST", "/__mock/touch?id=2718")
    document = try await storing.document(id: 2718) ?? document
    let second = try await storing.download(document: document, original: false)
    let after = try await h.json("GET", "/__mock/stats")["not_modified"] as? Int ?? 0
    try check(first == second, "path changed")
    try check(after == before + 1, "expected one 304, got \(after - before)")
    let original = try await storing.download(document: document, original: true)
    let originalKey = ContentStore.Key(
      serverID: serverID, versionID: document.currentVersionID, kind: .original)
    let originalChecksum = await index.checksum(originalKey)
    try check(originalChecksum == meta.originalChecksum, "original checksum mismatch")
    let size = (try? Data(contentsOf: original).count) ?? 0
    return "archive stored with ETag \(checksum?.prefix(12) ?? "nil")..., revalidated with a 304; original \(size) B"
  }
  await h.step("shareLinks: create / list / delete") {
    let link = try await repo.create(shareLink: ProtoShareLink(document: 2718, fileVersion: .archive))
    let links = try await repo.shareLinks(documentId: 2718)
    try check(links.contains { $0.id == link.id }, "created link is not listed")
    try await repo.delete(shareLink: link)
    let after = try await repo.shareLinks(documentId: 2718)
    try check(!after.contains { $0.id == link.id }, "link was not deleted")
    return "slug \(link.slug.prefix(10))..., listed, deleted"
  }

  // -- Mutations ------------------------------------------------------------

  await h.step("update(document:)") {
    let pristine = try await repo.document(id: 4242)
      ?? { throw CheckFailed(description: "nil") }()
    var document = pristine
    let modified = document.modified
    document.title = "Harness retitled"
    document.correspondent = correspondents[3].id
    document.tags = [tags[0].id]
    let updated = try await repo.update(document: document)
    try check(updated.title == "Harness retitled", "title \(updated.title)")
    try check(updated.correspondent == correspondents[3].id, "correspondent not updated")
    try check(updated.modified! > modified!, "modified was not bumped")
    try check(updated.permissions != nil, "PATCH response lacks permissions")
    let (hits, _) = try await count(
      defaultFilter.with {
        $0.searchText = "Harness retitled"
        $0.searchMode = .title
      })
    try check(hits == 1, "retitled document found \(hits) times")
    let reverted = try await repo.update(document: pristine)
    try check(
      reverted.title == pristine.title && reverted.tags == pristine.tags
        && reverted.correspondent == pristine.correspondent, "revert failed")
    return "title, correspondent and tags changed; modified bumped; found by new title; reverted"
  }
  await h.step("changed-documents delta (modified ascending from a watermark)") {
    let watermark = Date()
    let before = try await repo.document(id: 17011) ?? { throw CheckFailed(description: "nil") }()
    let touched = try await h.json("POST", "/__mock/touch?id=17011&title=Delta%20probe")
    try check(touched["touched"] as? Int == 1, "\(touched)")
    var filter = FilterState.empty
    filter.sortField = .modified
    filter.sortOrder = .ascending
    filter.date.modified = .between(start: watermark, end: nil)
    let source = try repo.documents(filter: filter)
    var changed: [Document] = []
    while true {
      let batch = try await source.fetch(limit: Endpoint.defaultDocumentPageSize)
      changed += batch
      let exhausted = await source.isExhausted
      if batch.isEmpty || exhausted { break }
    }
    // Put the original title back, so a second run sees the same library.
    _ = try await repo.update(document: before)
    try check(
      changed.contains { $0.id == 17011 && $0.title == "Delta probe" }, "touched document missing")
    try check(
      zip(changed, changed.dropFirst()).allSatisfy { $0.modified! <= $1.modified! }, "not ascending")
    let newer = changed.filter { $0.modified! >= watermark.addingTimeInterval(-1) }
    return "\(changed.count) documents modified since the watermark's day, \(newer.count) after the watermark itself, including #17011"
  }
  await h.step("delete(document:) / trash / restore") {
    let victim = try await repo.document(id: 4243) ?? { throw CheckFailed(description: "nil") }()
    try await repo.delete(document: victim)
    let ids = try await repo.documentIDs(filter: .empty)
    try check(!ids.contains(4243) && ids.count == allIDs.count - 1, "still listed")
    let gone = try await repo.document(id: 4243)
    try check(gone == nil, "deleted document still readable")
    let trash = try await repo.trash()
    try check(trash.contains { $0.id == 4243 }, "not in trash")
    try await repo.restoreTrash(documents: [4243])
    let restored = try await repo.documentIDs(filter: .empty)
    try check(restored.count == allIDs.count, "not restored")
    return "deleted, gone from fields=id, in trash (\(trash.count)), restored"
  }
  await h.step("tag create / update / delete") {
    let name = "Harness \(UUID().uuidString.prefix(8))"
    // An explicit sRGB colour: ProtoTag's default (`Color.gray`) is a dynamic
    // catalog colour, which `Color.hexString` cannot read on macOS.
    let color = try JSONDecoder().decode(HexColor.self, from: Data("\"#ff8800\"".utf8))
    let created = try await repo.create(
      tag: ProtoTag(name: name, color: color, parent: tags[1].id))
    let nesting = (repo.versions?.backendVersion).map { $0 >= Version(2, 19, 0) } ?? false
    try check(created.name == name, "created tag has the wrong name")
    try check(!nesting || created.parent == tags[1].id, "created tag has no parent")
    var changed = created
    changed.name = name + " renamed"
    changed.parent = nil
    let updated = try await repo.update(tag: changed)
    try check(updated.name == name + " renamed" && updated.parent == nil, "update wrong")
    let fetched = try await repo.tag(id: created.id)
    try check(fetched?.name == updated.name, "tag(id:) wrong")
    try await repo.delete(tag: updated)
    let after = try await repo.tags()
    try check(after.count == tags.count, "tag count \(after.count) != \(tags.count)")
    return "#\(created.id) created under #\(tags[1].id), renamed, deleted"
  }
  await h.step("correspondent / document type / storage path create + delete") {
    let suffix = String(UUID().uuidString.prefix(8))
    let correspondent = try await repo.create(
      correspondent: ProtoCorrespondent(name: "Harness Corr \(suffix)"))
    var renamed = correspondent
    renamed.name += " GmbH"
    let updated = try await repo.update(correspondent: renamed)
    try check(updated.name.hasSuffix("GmbH"), "rename failed")
    try await repo.delete(correspondent: updated)
    let type = try await repo.create(documentType: ProtoDocumentType(name: "Harness Type \(suffix)"))
    try await repo.delete(documentType: type)
    let path = try await repo.create(
      storagePath: ProtoStoragePath(name: "Harness Path \(suffix)", path: "{{ created_year }}/x"))
    try await repo.delete(storagePath: path)
    return "correspondent #\(correspondent.id), document type #\(type.id), storage path #\(path.id)"
  }
  await h.step("saved view create / update / delete") {
    let rule = FilterRule(ruleType: .hasTagsAll, value: .tag(id: tags[0].id))!
    let created = try await repo.create(
      savedView: ProtoSavedView(
        name: "Harness view \(UUID().uuidString.prefix(6))", showOnDashboard: true,
        sortField: .added, sortOrder: .ascending, filterRules: [rule]))
    try check(created.filterRules == [rule], "rules \(created.filterRules)")
    var changed = created
    changed.name += " v2"
    let updated = try await repo.update(savedView: changed)
    try check(updated.name.hasSuffix("v2"), "rename failed")
    try await repo.delete(savedView: updated)
    let after = try await repo.savedViews()
    try check(after.count == savedViews.count, "view was not deleted")
    return "#\(created.id) with \(created.filterRules.count) rule, sort \(created.sortField?.rawValue ?? "nil")"
  }
  await h.step("update(settings:) (POST /api/ui_settings/)") {
    var settings = try await repo.uiSettings().settings
    let before = settings.documentEditing.removeInboxTags
    settings.documentEditing.removeInboxTags = !before
    try await repo.update(settings: settings)
    let after = try await repo.uiSettings().settings
    try check(after.documentEditing.removeInboxTags == !before, "setting was not stored")
    try check(after.appTitle == settings.appTitle, "unmodelled settings were lost")
    settings.documentEditing.removeInboxTags = before
    try await repo.update(settings: settings)
    return "remove_inbox_tags toggled and restored"
  }
  await h.step("create(document:file:filename:) and tasks") {
    let file = FileManager.default.temporaryDirectory
      .appendingPathComponent("harness-upload-\(UUID().uuidString).pdf")
    try Data("%PDF-1.4 harness upload".utf8).write(to: file)
    defer { try? FileManager.default.removeItem(at: file) }
    let title = "Harness upload \(UUID().uuidString.prefix(6))"
    try await repo.create(
      document: ProtoDocument(
        title: title, correspondent: correspondents[0].id, tags: [tags[5].id]),
      file: file, filename: "harness.pdf")
    let listed = try await repo.tasks(limit: 50)
    let task = try listed.first { $0.taskFileName == "harness.pdf" }
      ?? { throw CheckFailed(description: "no task for the upload in \(listed.count) tasks") }()
    let single = try await repo.task(id: task.id)
    try check(single?.taskId == task.taskId, "task(id:) mismatch")
    let paged = try await repo.tasks().fetch(limit: 10)
    try check(!paged.isEmpty, "paged task source is empty")
    let (hits, page) = try await count(
      defaultFilter.with {
        $0.searchText = title
        $0.searchMode = .title
      })
    try check(hits == 1, "uploaded document found \(hits) times")
    try await repo.acknowledge(tasks: [task.id])
    let remaining = try await repo.tasks(limit: 50)
    try check(!remaining.contains { $0.id == task.id }, "task was not acknowledged")
    if let uploaded = page.first {
      try await repo.delete(document: uploaded)
      try await repo.emptyTrash(documents: [uploaded.id])
    }
    return "task \(task.status) for harness.pdf -> document #\(page.first?.id ?? 0), acknowledged, document removed again"
  }

  // -- Control endpoints ----------------------------------------------------

  await h.step("control: add / delete") {
    let added = try await h.json("POST", "/__mock/add?count=3")
    let first = added["first_id"] as? Int ?? 0
    let last = added["last_id"] as? Int ?? 0
    try check(added["added"] as? Int == 3 && last == first + 2, "\(added)")
    let ids = try await repo.documentIDs(filter: .empty)
    try check(ids.count == allIDs.count + 3 && ids.last == UInt(last), "new ids not listed")
    let fresh = try await repo.document(id: UInt(last)) ?? { throw CheckFailed(description: "nil") }()
    try check(fresh.added != nil, "no added date")
    _ = try await h.json("POST", "/__mock/delete?id=\(first)-\(last)&hard=1")
    let after = try await repo.documentIDs(filter: .empty)
    try check(after.count == allIDs.count, "added documents were not deleted")
    return "added #\(first)...#\(last) ('\(fresh.title)'), deleted again"
  }
  await h.step("control: fail mode 503, then off") {
    _ = try await h.json("POST", "/__mock/fail?mode=503")
    var failure: String?
    do {
      _ = try await repo.tags()
    } catch RequestError.unexpectedStatusCode(let code, _) {
      failure = "\(code)"
    } catch {
      failure = "other: \(error)"
    }
    _ = try await h.json("POST", "/__mock/fail?mode=off")
    try check(failure?.contains("503") == true || failure?.lowercased().contains("unavailable") == true,
      "expected a 503, got \(failure ?? "success")")
    let tagsAgain = try await repo.tags()
    try check(tagsAgain.count == tags.count, "not recovered")
    return "RequestError.unexpectedStatusCode(\(failure ?? "")), recovered after mode=off"
  }
  await h.step("control: stats, no unknown parameters or endpoints") {
    let stats = try await h.json("GET", "/__mock/stats")
    let unknownParams = stats["unknown_params"] as? [String: Any] ?? [:]
    let unknownEndpoints = stats["unknown_endpoints"] as? [String: Any] ?? [:]
    try check(unknownParams.isEmpty, "unknown query parameters: \(unknownParams.keys.sorted())")
    try check(unknownEndpoints.isEmpty, "unknown endpoints: \(unknownEndpoints.keys.sorted())")
    return
      "\(stats["requests"] ?? 0) requests, \(stats["bytes_served"] ?? 0) bytes, \(stats["pdfs_served"] ?? 0) PDFs, \(stats["thumbnails_served"] ?? 0) thumbnails"
  }

  print("\n\(h.passed) passed, \(h.failed) failed")
  return h.failed == 0 ? 0 : 1
}

exit(await run())
