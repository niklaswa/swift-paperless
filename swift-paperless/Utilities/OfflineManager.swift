//
//  OfflineManager.swift
//  swift-paperless
//
//  Created for offline document support
//

import Combine
import DataModel
import Foundation
import Network
import Networking
import os

/// Represents the current state of offline document downloads
public struct OfflineDownloadState: Equatable, Sendable {
  public var totalDocuments: Int = 0
  public var downloadedDocuments: Int = 0
  public var currentDownloadProgress: Double = 0
  public var bytesDownloaded: Int64 = 0
  public var downloadSpeed: Double = 0  // bytes per second
  public var isDownloading: Bool = false
  public var lastError: String?

  public var overallProgress: Double {
    guard totalDocuments > 0 else { return 0 }
    let completedProgress = Double(downloadedDocuments) / Double(totalDocuments)
    let currentProgress = currentDownloadProgress / Double(totalDocuments)
    return completedProgress + currentProgress
  }

  public var progressPercentage: Int {
    Int(overallProgress * 100)
  }
}

/// Cached metadata for offline use
public struct OfflineMetadataCache: Codable, Sendable {
  public var documents: [UInt: Document]
  public var correspondents: [UInt: Correspondent]
  public var documentTypes: [UInt: DocumentType]
  public var tags: [UInt: Tag]
  public var storagePaths: [UInt: StoragePath]
  public var lastUpdated: Date
  
  public init() {
    self.documents = [:]
    self.correspondents = [:]
    self.documentTypes = [:]
    self.tags = [:]
    self.storagePaths = [:]
    self.lastUpdated = Date()
  }
}

/// Manages offline document storage and network connectivity monitoring
@MainActor
public final class OfflineManager: ObservableObject {
  // MARK: - Published Properties

  @Published public private(set) var isOnline: Bool = true
  @Published public private(set) var downloadState = OfflineDownloadState()
  @Published public private(set) var metadataCache = OfflineMetadataCache()
  
  // Cached values to avoid frequent file system reads
  @Published public private(set) var cachedDownloadedIds: Set<UInt> = []
  @Published public private(set) var cachedTotalSize: Int64 = 0
  
  @Published public var offlineModeEnabled: Bool {
    didSet {
      saveOfflineModeEnabled()
      if offlineModeEnabled {
        startDownloadingDocuments()
      } else {
        cancelDownload()
      }
    }
  }
  
  /// Returns the UserDefaults key for offline mode enabled setting
  private var offlineModeEnabledKey: String {
    if let connectionId = currentConnectionId {
      return "offlineModeEnabled_\(connectionId.uuidString)"
    }
    return "offlineModeEnabled"
  }
  
  private func saveOfflineModeEnabled() {
    UserDefaults.standard.set(offlineModeEnabled, forKey: offlineModeEnabledKey)
  }
  
  private func loadOfflineModeEnabled() {
    offlineModeEnabled = UserDefaults.standard.bool(forKey: offlineModeEnabledKey)
  }

  // MARK: - Private Properties

  private let networkMonitor = NWPathMonitor()
  private let monitorQueue = DispatchQueue(label: "com.swift-paperless.networkMonitor")
  private var downloadTask: Task<Void, Never>?
  private var speedTracker = SpeedTracker()
  private weak var store: DocumentStore?
  private var lastProgressUpdate: Date = .distantPast

  private var offlineDirectory: URL
  private var metadataCacheFile: URL
  private var cacheLoadTask: Task<Void, Never>?
  private var valuesRefreshTask: Task<Void, Never>?
  
  /// The currently active connection ID, used to separate storage between servers
  private var currentConnectionId: UUID?

  // MARK: - Initialization

  public init() {
    // Default to false; actual value will be loaded when connection is configured
    self.offlineModeEnabled = false

    // Create base offline storage directory (will be updated when connection is set)
    let documentsPath = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
    let baseDirectory = documentsPath.appendingPathComponent("OfflineDocuments", isDirectory: true)
    self.offlineDirectory = baseDirectory
    self.metadataCacheFile = baseDirectory.appendingPathComponent("OfflineMetadata.json")

    try? FileManager.default.createDirectory(at: offlineDirectory, withIntermediateDirectories: true)

    // Cache loading will be triggered when connection is set via configure()
    startNetworkMonitoring()
  }
  
  /// Ensure that offline cache is fully loaded. Call this before accessing offline data.
  public func ensureCacheLoaded() async {
    await cacheLoadTask?.value
    await valuesRefreshTask?.value
  }

  deinit {
    networkMonitor.cancel()
  }

  // MARK: - Configuration

  /// Updates the storage directory for a specific connection and loads its cache
  func configure(with store: DocumentStore, connectionId: UUID? = nil) {
    self.store = store
    
    // If connection ID changed, switch to that connection's storage
    if let connectionId = connectionId, connectionId != currentConnectionId {
      switchToConnection(connectionId)
    } else if currentConnectionId == nil, let connectionId = connectionId {
      switchToConnection(connectionId)
    }
  }
  
  /// Switch storage to a different connection's directory
  private func switchToConnection(_ connectionId: UUID) {
    Logger.shared.info("Switching offline storage to connection: \(connectionId)")
    
    // Cancel any ongoing downloads
    cancelDownload()
    
    currentConnectionId = connectionId
    
    // Load the offline mode setting for this connection
    loadOfflineModeEnabled()
    
    // Create connection-specific directory
    let documentsPath = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
    let baseDirectory = documentsPath.appendingPathComponent("OfflineDocuments", isDirectory: true)
    offlineDirectory = baseDirectory.appendingPathComponent(connectionId.uuidString, isDirectory: true)
    metadataCacheFile = offlineDirectory.appendingPathComponent("OfflineMetadata.json")
    
    try? FileManager.default.createDirectory(at: offlineDirectory, withIntermediateDirectories: true)
    
    // Reset state for new connection
    metadataCache = OfflineMetadataCache()
    downloadState = OfflineDownloadState()
    cachedDownloadedIds = []
    cachedTotalSize = 0
    
    // Load cached metadata for this connection
    let cacheFile = metadataCacheFile
    cacheLoadTask = Task {
      let cache = await Task.detached {
        Self.loadMetadataCacheFromDisk(at: cacheFile)
      }.value
      if let cache = cache {
        self.metadataCache = cache
      }
    }
    
    // Refresh cached values
    valuesRefreshTask = Task {
      await self.refreshCachedValues()
    }
    
    // Check if offline mode should start downloading
    if offlineModeEnabled {
      Task {
        await ensureCacheLoaded()
        startDownloadingDocuments()
      }
    }
  }
  
  /// Legacy configure method for backwards compatibility
  func configure(with store: DocumentStore) {
    self.store = store
  }

  // MARK: - Public Methods

  /// Returns the offline file URL for a document if it exists
  public func offlineFileURL(for documentId: UInt) -> URL? {
    let fileURL = offlineDirectory.appendingPathComponent("\(documentId).pdf")
    return FileManager.default.fileExists(atPath: fileURL.path) ? fileURL : nil
  }

  /// Returns whether a document is available offline
  public func isDocumentAvailableOffline(_ documentId: UInt) -> Bool {
    offlineFileURL(for: documentId) != nil
  }

  /// Get all downloaded document IDs
  public func downloadedDocumentIds() -> Set<UInt> {
    do {
      let files = try FileManager.default.contentsOfDirectory(at: offlineDirectory, includingPropertiesForKeys: nil)
      return Set(files.compactMap { url -> UInt? in
        let filename = url.deletingPathExtension().lastPathComponent
        return UInt(filename)
      })
    } catch {
      Logger.shared.error("Error reading offline directory: \(error)")
      return []
    }
  }

  /// Get the total size of offline documents
  public func totalOfflineSize() -> Int64 {
    do {
      let files = try FileManager.default.contentsOfDirectory(at: offlineDirectory, includingPropertiesForKeys: [.fileSizeKey])
      return files.reduce(0) { total, url in
        let size = (try? url.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0
        return total + Int64(size)
      }
    } catch {
      Logger.shared.error("Error calculating offline size: \(error)")
      return 0
    }
  }

  /// Delete all offline documents
  public func clearOfflineDocuments() {
    cancelDownload()
    do {
      let files = try FileManager.default.contentsOfDirectory(at: offlineDirectory, includingPropertiesForKeys: nil)
      for file in files {
        try FileManager.default.removeItem(at: file)
      }
      // Also clear metadata cache
      try? FileManager.default.removeItem(at: metadataCacheFile)
      metadataCache = OfflineMetadataCache()
      downloadState = OfflineDownloadState()
      cachedDownloadedIds = []
      cachedTotalSize = 0
      Logger.shared.info("Cleared all offline documents and metadata")
    } catch {
      Logger.shared.error("Error clearing offline documents: \(error)")
    }
  }

  /// Start downloading all documents for offline use
  public func startDownloadingDocuments() {
    guard offlineModeEnabled, isOnline, downloadTask == nil else { return }
    guard let store = store else {
      Logger.shared.error("OfflineManager: No store configured")
      return
    }
    
    // Capture store data for metadata caching
    cacheMetadata(from: store)
    
    // Capture repository reference before creating task
    let repository = store.repository
    
    downloadTask = Task { [weak self] in
      await self?.downloadAllDocuments(using: repository)
    }
  }

  /// Cancel the current download
  public func cancelDownload() {
    downloadTask?.cancel()
    downloadTask = nil
    downloadState.isDownloading = false
  }
  
  /// Get cached documents for offline search
  public func cachedDocuments() -> [Document] {
    Array(metadataCache.documents.values)
  }
  
  /// Get a cached document by ID
  public func cachedDocument(id: UInt) -> Document? {
    metadataCache.documents[id]
  }
  
  /// Get a cached correspondent by ID
  public func cachedCorrespondent(id: UInt) -> Correspondent? {
    metadataCache.correspondents[id]
  }
  
  /// Get a cached tag by ID
  public func cachedTag(id: UInt) -> Tag? {
    metadataCache.tags[id]
  }
  
  /// Get a cached document type by ID
  public func cachedDocumentType(id: UInt) -> DocumentType? {
    metadataCache.documentTypes[id]
  }
  
  /// Get a cached storage path by ID
  public func cachedStoragePath(id: UInt) -> StoragePath? {
    metadataCache.storagePaths[id]
  }
  
  /// Get all cached correspondents
  public func cachedCorrespondents() -> [Correspondent] {
    Array(metadataCache.correspondents.values)
  }
  
  /// Get all cached tags
  public func cachedTags() -> [Tag] {
    Array(metadataCache.tags.values)
  }
  
  /// Get all cached document types
  public func cachedDocumentTypes() -> [DocumentType] {
    Array(metadataCache.documentTypes.values)
  }
  
  /// Get all cached storage paths
  public func cachedStoragePaths() -> [StoragePath] {
    Array(metadataCache.storagePaths.values)
  }
  
  /// Search cached documents by title (for offline search)
  public func searchDocuments(query: String) -> [Document] {
    guard !query.isEmpty else { return cachedDocuments() }
    let lowercasedQuery = query.lowercased()
    return cachedDocuments().filter { doc in
      doc.title.lowercased().contains(lowercasedQuery)
    }
  }
  
  /// Refresh cached values (downloaded IDs and total size) from file system
  /// Call this after downloads complete or files are deleted
  public func refreshCachedValues() async {
    let directory = offlineDirectory
    
    // Do file system work off main thread
    let (ids, size) = await Task.detached {
      var downloadedIds: Set<UInt> = []
      var totalSize: Int64 = 0
      
      do {
        let files = try FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: [.fileSizeKey])
        for url in files {
          if let id = UInt(url.deletingPathExtension().lastPathComponent) {
            downloadedIds.insert(id)
          }
          if let fileSize = try? url.resourceValues(forKeys: [.fileSizeKey]).fileSize {
            totalSize += Int64(fileSize)
          }
        }
      } catch {
        Logger.shared.error("Error reading offline directory: \(error)")
      }
      
      return (downloadedIds, totalSize)
    }.value
    
    await MainActor.run {
      self.cachedDownloadedIds = ids
      self.cachedTotalSize = size
    }
  }

  // MARK: - Private Methods
  
  /// Load metadata cache from disk (can be called from any thread)
  private nonisolated static func loadMetadataCacheFromDisk(at url: URL) -> OfflineMetadataCache? {
    guard FileManager.default.fileExists(atPath: url.path) else { return nil }
    
    do {
      let data = try Data(contentsOf: url)
      let decoder = JSONDecoder()
      decoder.dateDecodingStrategy = .iso8601
      let cache = try decoder.decode(OfflineMetadataCache.self, from: data)
      Logger.shared.info("Loaded offline metadata cache with \(cache.documents.count) documents")
      return cache
    } catch {
      Logger.shared.error("Error loading metadata cache: \(error)")
      return nil
    }
  }
  
  private func saveMetadataCache() {
    do {
      let encoder = JSONEncoder()
      encoder.dateEncodingStrategy = .iso8601
      let data = try encoder.encode(metadataCache)
      try data.write(to: metadataCacheFile)
      Logger.shared.info("Saved offline metadata cache with \(self.metadataCache.documents.count) documents")
    } catch {
      Logger.shared.error("Error saving metadata cache: \(error)")
    }
  }
  
  private func cacheMetadata(from store: DocumentStore) {
    // Cache all the metadata from the store
    metadataCache.correspondents = store.correspondents
    metadataCache.documentTypes = store.documentTypes
    metadataCache.tags = store.tags
    metadataCache.storagePaths = store.storagePaths
    metadataCache.lastUpdated = Date()
    saveMetadataCache()
  }

  private func startNetworkMonitoring() {
    networkMonitor.pathUpdateHandler = { [weak self] path in
      Task { @MainActor [weak self] in
        let wasOnline = self?.isOnline ?? true
        self?.isOnline = path.status == .satisfied

        if let self = self, !wasOnline && self.isOnline && self.offlineModeEnabled {
          self.startDownloadingDocuments()
        }
      }
    }
    networkMonitor.start(queue: monitorQueue)
  }

  private func downloadAllDocuments(using repository: any Repository) async {
    await MainActor.run {
      downloadState.isDownloading = true
      downloadState.lastError = nil
      speedTracker.reset()
    }

    do {
      // Fetch all document IDs
      let documents = try await repository.documents(filter: .default).fetch(limit: 100_000)
      let downloadedIds = await MainActor.run { cachedDownloadedIds }

      let documentsToDownload = documents.filter { !downloadedIds.contains($0.id) }
      
      // Cache all document metadata
      await MainActor.run {
        for doc in documents {
          metadataCache.documents[doc.id] = doc
        }
      }
      
      // Fetch and cache correspondents, tags, document types, storage paths
      do {
        let correspondents = try await repository.correspondents()
        await MainActor.run {
          for correspondent in correspondents {
            metadataCache.correspondents[correspondent.id] = correspondent
          }
        }
      } catch {
        Logger.shared.warning("Failed to fetch correspondents for offline cache: \(error)")
      }
      
      do {
        let tags = try await repository.tags()
        await MainActor.run {
          for tag in tags {
            metadataCache.tags[tag.id] = tag
          }
        }
      } catch {
        Logger.shared.warning("Failed to fetch tags for offline cache: \(error)")
      }
      
      do {
        let documentTypes = try await repository.documentTypes()
        await MainActor.run {
          for docType in documentTypes {
            metadataCache.documentTypes[docType.id] = docType
          }
        }
      } catch {
        Logger.shared.warning("Failed to fetch document types for offline cache: \(error)")
      }
      
      do {
        let storagePaths = try await repository.storagePaths()
        await MainActor.run {
          for storagePath in storagePaths {
            metadataCache.storagePaths[storagePath.id] = storagePath
          }
        }
      } catch {
        Logger.shared.warning("Failed to fetch storage paths for offline cache: \(error)")
      }
      
      // Save all metadata
      await MainActor.run {
        saveMetadataCache()
      }

      await MainActor.run {
        downloadState.totalDocuments = documents.count
        downloadState.downloadedDocuments = downloadedIds.count
      }

      for document in documentsToDownload {
        guard !Task.isCancelled else { break }
        
        let online = await MainActor.run { isOnline }
        guard online else {
          await MainActor.run {
            downloadState.lastError = "Network connection lost"
          }
          break
        }

        await downloadDocument(document, repository: repository)
      }
    } catch {
      Logger.shared.error("Error fetching documents for offline download: \(error)")
      await MainActor.run {
        downloadState.lastError = error.localizedDescription
      }
    }

    await MainActor.run {
      downloadState.isDownloading = false
      downloadTask = nil
    }
  }

  private func downloadDocument(_ document: Document, repository: any Repository) async {
    let fileURL = offlineDirectory.appendingPathComponent("\(document.id).pdf")

    // Skip if already downloaded
    if FileManager.default.fileExists(atPath: fileURL.path) {
      await MainActor.run {
        downloadState.downloadedDocuments += 1
      }
      return
    }

    await MainActor.run {
      downloadState.currentDownloadProgress = 0
    }

    do {
      let startTime = Date()
      var lastBytes: Int64 = 0

      guard let downloadedURL = try await repository.download(
        documentID: document.id,
        progress: { [weak self] (progress: Double) in
          // Throttle progress updates to reduce UI churn
          let now = Date()
          Task { @MainActor [weak self] in
            guard let self = self else { return }
            
            // Only update UI at most every 0.5 seconds
            guard now.timeIntervalSince(self.lastProgressUpdate) >= 0.5 else { return }
            self.lastProgressUpdate = now
            
            self.downloadState.currentDownloadProgress = progress

            // Estimate bytes based on progress (rough estimate)
            let estimatedBytes = Int64(progress * 1_000_000)  // Assume ~1MB average
            let bytesDelta = estimatedBytes - lastBytes
            lastBytes = estimatedBytes

            self.speedTracker.addBytes(bytesDelta)
            self.downloadState.downloadSpeed = self.speedTracker.currentSpeed
            self.downloadState.bytesDownloaded += bytesDelta
          }
        }
      ) else {
        Logger.shared.warning("Failed to download document \(document.id)")
        return
      }

      // Move to offline storage
      try FileManager.default.moveItem(at: downloadedURL, to: fileURL)

      // Update actual file size
      if let fileSize = try? fileURL.resourceValues(forKeys: [.fileSizeKey]).fileSize {
        let actualBytes = Int64(fileSize)
        await MainActor.run {
          downloadState.bytesDownloaded = downloadState.bytesDownloaded - lastBytes + actualBytes
          cachedTotalSize += actualBytes
        }
      }

      await MainActor.run {
        downloadState.downloadedDocuments += 1
        downloadState.currentDownloadProgress = 0
        cachedDownloadedIds.insert(document.id)
      }

      let duration = Date().timeIntervalSince(startTime)
      Logger.shared.debug("Downloaded document \(document.id) in \(duration)s")

    } catch {
      Logger.shared.error("Error downloading document \(document.id): \(error)")
    }
  }
}

// MARK: - Speed Tracker

private class SpeedTracker {
  private var samples: [(timestamp: Date, bytes: Int64)] = []
  private let windowDuration: TimeInterval = 3.0  // 3 second rolling window
  private var smoothedSpeed: Double = 0
  private let smoothingFactor: Double = 0.3  // Lower = smoother, higher = more responsive

  var currentSpeed: Double {
    cleanOldSamples()
    guard samples.count >= 2 else { return smoothedSpeed }

    let totalBytes = samples.reduce(0) { $0 + $1.bytes }
    let duration = Date().timeIntervalSince(samples.first?.timestamp ?? Date())

    guard duration > 0 else { return smoothedSpeed }
    
    let instantSpeed = Double(totalBytes) / duration
    
    // Apply exponential moving average for smooth transitions
    if smoothedSpeed == 0 {
      smoothedSpeed = instantSpeed
    } else {
      smoothedSpeed = smoothingFactor * instantSpeed + (1 - smoothingFactor) * smoothedSpeed
    }
    
    return smoothedSpeed
  }

  func addBytes(_ bytes: Int64) {
    samples.append((timestamp: Date(), bytes: bytes))
    cleanOldSamples()
  }

  func reset() {
    samples.removeAll()
    smoothedSpeed = 0
  }

  private func cleanOldSamples() {
    let cutoff = Date().addingTimeInterval(-windowDuration)
    samples.removeAll { $0.timestamp < cutoff }
  }
}

// MARK: - Formatting Helpers

extension OfflineManager {
  /// Format bytes as human-readable string
  public static func formatBytes(_ bytes: Int64) -> String {
    let formatter = ByteCountFormatter()
    formatter.countStyle = .file
    return formatter.string(fromByteCount: bytes)
  }

  /// Format speed as human-readable string
  public static func formatSpeed(_ bytesPerSecond: Double) -> String {
    let formatter = ByteCountFormatter()
    formatter.countStyle = .file
    return "\(formatter.string(fromByteCount: Int64(bytesPerSecond)))/s"
  }
}
