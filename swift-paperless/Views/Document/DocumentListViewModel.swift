//
//  DocumentListViewModel.swift
//  swift-paperless
//
//  Created by Paul Gessinger on 14.05.2024.
//

import DataModel
import Foundation
import Networking
import Nuke
import Observation
import SwiftUI
import os

@MainActor
@Observable
class DocumentListViewModel {
  private var store: DocumentStore
  private var filterState: FilterState
  private var errorController: ErrorController
  weak var offlineManager: OfflineManager?

  var documents: [Document] = []
  var ready = false

  var noPermissions = false
  var isOfflineMode = false

  private var source: (any DocumentSource)?
  private var exhausted: Bool = false

  private var initialBatchSize: UInt = 250
  private var batchSize: UInt = 250
  private var fetchMargin = 10

  private var imagePrefetcher: ImagePrefetcher

  init(store: DocumentStore, filterState: FilterState, errorController: ErrorController, offlineManager: OfflineManager? = nil) {
    self.store = store
    self.filterState = filterState
    self.errorController = errorController
    self.offlineManager = offlineManager

    let dataloader = DataLoader()

    if let delegate = store.repository.delegate {
      dataloader.delegate = delegate
    }

    imagePrefetcher = ImagePrefetcher(
      pipeline: ImagePipeline(configuration: .init(dataLoader: dataloader)))

    imagePrefetcher.didComplete = {
      Logger.shared.debug("Thumbnail prefetching completed")
    }
  }
  
  func setOfflineManager(_ manager: OfflineManager) {
    self.offlineManager = manager
  }

  func reload() async {
    Logger.shared.debug("DocumentListViewModel.reload")
    documents = []
    source = nil
    await load()
    try? await Task.sleep(for: .seconds(0.1))
    ready = true
  }

  private func ensurePermissions() throws {
    guard store.permissions.test(.view, for: .document) else {
      throw PermissionsError(resource: .document, operation: .view)
    }
    noPermissions = false
  }

  func load() async {
    Logger.shared.debug("DocumentListViewModel.load")
    guard documents.isEmpty else { return }
    
    // Check if we're offline
    if let offlineManager = offlineManager, !offlineManager.isOnline {
      isOfflineMode = true
      // Ensure cache is loaded before accessing offline data
      await offlineManager.ensureCacheLoaded()
      loadOfflineDocuments()
      ready = true
      return
    }
    
    isOfflineMode = false
    
    do {
      // Ensure we have up-to-date permissions
      try await store.fetchUISettings()
      if source == nil {
        source = try await store.repository.documents(filter: filterState)
      }
      try ensurePermissions()
      let batch = try await source!.fetch(limit: initialBatchSize)

      let requests =
        try batch
        .map { try store.repository.thumbnailRequest(document: $0) }
        .map { ImageRequest(urlRequest: $0, processors: [.resize(width: 130)]) }

      Logger.shared.debug("Prefetching \(requests.count) thumbnail images")
      imagePrefetcher.startPrefetching(with: requests)

      documents = batch
      Logger.shared.debug("DocumentListViewModel.load loading complete")
    } catch let error as PermissionsError {
      noPermissions = true
      Logger.shared.warning("Insufficient permissions to load documents: \(error)")
    } catch {
      Logger.shared.error("DocumentList failed to load documents: \(error)")
      
      // If loading fails and we have offline data, use it
      if let offlineManager = offlineManager {
        await offlineManager.ensureCacheLoaded()
        if !offlineManager.metadataCache.documents.isEmpty {
          isOfflineMode = true
          loadOfflineDocuments()
        } else {
          errorController.push(error: error)
        }
      } else {
        errorController.push(error: error)
      }
    }
  }
  
  private func loadOfflineDocuments() {
    guard let offlineManager = offlineManager else { return }
    
    // Get downloaded document IDs - use cached if available, otherwise compute
    let downloadedIds: Set<UInt>
    if !offlineManager.cachedDownloadedIds.isEmpty {
      downloadedIds = offlineManager.cachedDownloadedIds
    } else {
      // Fallback: compute from file system (slower but guaranteed to work)
      downloadedIds = offlineManager.downloadedDocumentIds()
    }
    
    // Get cached documents that are actually downloaded
    var cachedDocs = offlineManager.cachedDocuments().filter { downloadedIds.contains($0.id) }
    
    // Apply correspondent filter
    switch filterState.correspondent {
    case .any:
      break
    case .notAssigned:
      cachedDocs = cachedDocs.filter { $0.correspondent == nil }
    case .anyOf(let ids):
      cachedDocs = cachedDocs.filter { doc in
        guard let correspondentId = doc.correspondent else { return false }
        return ids.contains(correspondentId)
      }
    case .noneOf(let ids):
      cachedDocs = cachedDocs.filter { doc in
        guard let correspondentId = doc.correspondent else { return true }
        return !ids.contains(correspondentId)
      }
    }
    
    // Apply document type filter
    switch filterState.documentType {
    case .any:
      break
    case .notAssigned:
      cachedDocs = cachedDocs.filter { $0.documentType == nil }
    case .anyOf(let ids):
      cachedDocs = cachedDocs.filter { doc in
        guard let docTypeId = doc.documentType else { return false }
        return ids.contains(docTypeId)
      }
    case .noneOf(let ids):
      cachedDocs = cachedDocs.filter { doc in
        guard let docTypeId = doc.documentType else { return true }
        return !ids.contains(docTypeId)
      }
    }
    
    // Apply storage path filter
    switch filterState.storagePath {
    case .any:
      break
    case .notAssigned:
      cachedDocs = cachedDocs.filter { $0.storagePath == nil }
    case .anyOf(let ids):
      cachedDocs = cachedDocs.filter { doc in
        guard let pathId = doc.storagePath else { return false }
        return ids.contains(pathId)
      }
    case .noneOf(let ids):
      cachedDocs = cachedDocs.filter { doc in
        guard let pathId = doc.storagePath else { return true }
        return !ids.contains(pathId)
      }
    }
    
    // Apply tags filter
    switch filterState.tags {
    case .any:
      break
    case .notAssigned:
      cachedDocs = cachedDocs.filter { $0.tags.isEmpty }
    case .allOf(let include, let exclude):
      cachedDocs = cachedDocs.filter { doc in
        // Must have all included tags
        for tagId in include {
          if !doc.tags.contains(tagId) {
            return false
          }
        }
        // Must not have any excluded tags
        for tagId in exclude {
          if doc.tags.contains(tagId) {
            return false
          }
        }
        return true
      }
    case .anyOf(let ids):
      cachedDocs = cachedDocs.filter { doc in
        for tagId in ids {
          if doc.tags.contains(tagId) {
            return true
          }
        }
        return false
      }
    }
    
    // Apply search filter if present
    if !filterState.searchText.isEmpty {
      let query = filterState.searchText.lowercased()
      cachedDocs = cachedDocs.filter { doc in
        doc.title.lowercased().contains(query)
      }
    }
    
    // Apply sorting
    cachedDocs.sort { (doc1: Document, doc2: Document) -> Bool in
      switch filterState.sortField {
      case .added:
        let a1 = doc1.added ?? .distantPast
        let a2 = doc2.added ?? .distantPast
        return filterState.sortOrder.reverse 
          ? a1 > a2 
          : a1 < a2
      case .created:
        return filterState.sortOrder.reverse 
          ? doc1.created > doc2.created 
          : doc1.created < doc2.created
      case .modified:
        let m1 = doc1.modified ?? .distantPast
        let m2 = doc2.modified ?? .distantPast
        return filterState.sortOrder.reverse 
          ? m1 > m2 
          : m1 < m2
      case .title:
        return filterState.sortOrder.reverse 
          ? doc1.title > doc2.title 
          : doc1.title < doc2.title
      case .asn:
        let asn1 = doc1.asn ?? 0
        let asn2 = doc2.asn ?? 0
        return filterState.sortOrder.reverse 
          ? asn1 > asn2 
          : asn1 < asn2
      case .correspondent:
        let c1 = doc1.correspondent ?? 0
        let c2 = doc2.correspondent ?? 0
        return filterState.sortOrder.reverse 
          ? c1 > c2 
          : c1 < c2
      case .documentType:
        let dt1 = doc1.documentType ?? 0
        let dt2 = doc2.documentType ?? 0
        return filterState.sortOrder.reverse 
          ? dt1 > dt2 
          : dt1 < dt2
      case .storagePath:
        let sp1 = doc1.storagePath ?? 0
        let sp2 = doc2.storagePath ?? 0
        return filterState.sortOrder.reverse 
          ? sp1 > sp2 
          : sp1 < sp2
      case .score, .pageCount, .owner, .notes, .customField, .other:
        // Fall back to sorting by added date for unsupported fields
        let a1 = doc1.added ?? .distantPast
        let a2 = doc2.added ?? .distantPast
        return filterState.sortOrder.reverse 
          ? a1 > a2 
          : a1 < a2
      }
    }
    
    documents = cachedDocs
    exhausted = true
    Logger.shared.info("Loaded \(cachedDocs.count) offline documents")
  }

  func fetchMoreIfNeeded(currentIndex: Int) async {
    // Don't fetch more when in offline mode
    if isOfflineMode || exhausted { return }
    if currentIndex >= documents.count - fetchMargin {
      let repository = store.repository
      Task.detached {
        do {
          Logger.shared.info("Fetching additional documents")
          guard let source = await self.source else {
            return
          }

          let batch = try await source.fetch(limit: self.batchSize)
          if batch.isEmpty {
            await MainActor.run {
              self.exhausted = true
            }
            return
          }

          let requests =
            try batch
            .map { try repository.thumbnailRequest(document: $0) }
            .map { ImageRequest(urlRequest: $0, processors: [.resize(width: 130)]) }

          Logger.shared.debug("Prefetching \(requests.count) thumbnail images")
          await self.imagePrefetcher.startPrefetching(with: requests)

          await MainActor.run {
            self.documents += batch
          }
        } catch {
          Logger.shared.error("DocumentList failed to load more if needed: \(error)")
          await self.errorController.push(error: error)
        }
      }
    }
  }

  func refresh(filter: FilterState? = nil, retain: Bool = false) async throws -> [Document] {
    // Check if we're offline
    if let offlineManager = offlineManager, !offlineManager.isOnline {
      if let filter {
        filterState = filter
      }
      isOfflineMode = true
      // Ensure cache is loaded before accessing offline data
      await offlineManager.ensureCacheLoaded()
      loadOfflineDocuments()
      return documents
    }
    
    isOfflineMode = false
    try await store.fetchAll()

    if let filter {
      filterState = filter
    }
    exhausted = false
    do {
      try ensurePermissions()
      source = try await store.repository.documents(filter: filterState)

      let batch = try await source!.fetch(limit: retain ? UInt(documents.count) : initialBatchSize)

      let requests =
        try batch
        .map { try self.store.repository.thumbnailRequest(document: $0) }
        .map { ImageRequest(urlRequest: $0, processors: [.resize(width: 130)]) }

      Logger.shared.debug("Prefetching \(requests.count) thumbnail images")
      imagePrefetcher.startPrefetching(with: requests)

      return batch
    } catch let error as PermissionsError {
      Logger.shared.error("Insufficient permissions to refresh documents: \(error)")
      noPermissions = true
      throw error
    } catch {
      Logger.shared.error("DocumentList failed to refresh: \(error)")
      errorController.push(error: error)
      throw error
    }
  }

  func replace(documents: [Document]) {
    self.documents = documents
  }

  func removed(document: Document) {
    documents.removeAll(where: { $0.id == document.id })
  }

  func updated(document: Document) {
    if let target = documents.firstIndex(where: { $0.id == document.id }) {
      documents[target] = document
    }
  }

  func removeInboxTags(document: Document) async {
    var document = document
    let inboxTags = store.tags.values.filter(\.isInboxTag)
    for tag in inboxTags {
      document.tags.removeAll(where: { $0 == tag.id })
    }
    _ = try? await store.updateDocument(document)
  }
}
