//
//  OfflineSettingsView.swift
//  swift-paperless
//
//  Created for offline document support
//

import SwiftUI

struct OfflineSettingsView: View {
  @EnvironmentObject private var offlineManager: OfflineManager
  @State private var showClearConfirmation = false

  private var downloadedCount: Int {
    offlineManager.cachedDownloadedIds.count
  }

  private var totalSize: String {
    OfflineManager.formatBytes(offlineManager.cachedTotalSize)
  }

  var body: some View {
    Form {
      Section {
        Toggle(isOn: $offlineManager.offlineModeEnabled) {
          Label {
            Text(.settings(.offlineModeToggle))
          } icon: {
            Image(systemName: "arrow.down.circle.fill")
          }
        }
      } footer: {
        Text(.settings(.offlineModeDescription))
      }

      Section {
        // Connection status
        HStack {
          Label {
            Text(.settings(.connectionStatus))
          } icon: {
            Image(systemName: offlineManager.isOnline ? "wifi" : "wifi.slash")
              .foregroundStyle(offlineManager.isOnline ? .green : .red)
          }
          Spacer()
          Text(offlineManager.isOnline
               ? String(localized: .settings(.statusOnline))
               : String(localized: .settings(.statusOffline)))
            .foregroundStyle(.secondary)
        }

        // Downloaded documents count
        HStack {
          Label {
            Text(.settings(.downloadedDocuments))
          } icon: {
            Image(systemName: "doc.fill")
          }
          Spacer()
          Text("\(downloadedCount)")
            .foregroundStyle(.secondary)
        }

        // Storage used
        HStack {
          Label {
            Text(.settings(.storageUsed))
          } icon: {
            Image(systemName: "internaldrive.fill")
          }
          Spacer()
          Text(totalSize)
            .foregroundStyle(.secondary)
        }
      } header: {
        Text(.settings(.offlineStatus))
      }

      if offlineManager.offlineModeEnabled {
        Section {
          if offlineManager.downloadState.isDownloading {
            downloadProgressView
          } else if let error = offlineManager.downloadState.lastError {
            // Show error with retry button
            VStack(alignment: .leading, spacing: 8) {
              HStack {
                Image(systemName: "exclamationmark.triangle.fill")
                  .foregroundStyle(.yellow)
                Text(error)
                  .font(.caption)
                  .foregroundStyle(.secondary)
              }
              
              Button {
                offlineManager.startDownloadingDocuments()
              } label: {
                Label {
                  Text(.settings(.retryDownload))
                } icon: {
                  Image(systemName: "arrow.clockwise")
                }
              }
            }
          } else if offlineManager.downloadState.downloadedDocuments < offlineManager.downloadState.totalDocuments {
            Button {
              offlineManager.startDownloadingDocuments()
            } label: {
              Label {
                Text(.settings(.resumeDownload))
              } icon: {
                Image(systemName: "arrow.clockwise")
              }
            }
          } else if offlineManager.downloadState.totalDocuments > 0 {
            Label {
              Text(.settings(.allDocumentsDownloaded))
            } icon: {
              Image(systemName: "checkmark.circle.fill")
                .foregroundStyle(.green)
            }
          }
        } header: {
          Text(.settings(.downloadProgress))
        }
      }

      Section {
        Button(role: .destructive) {
          showClearConfirmation = true
        } label: {
          Label {
            Text(.settings(.clearOfflineData))
          } icon: {
            Image(systemName: "trash.fill")
          }
        }
        .disabled(downloadedCount == 0)
      } footer: {
        Text(.settings(.clearOfflineDataDescription))
      }
    }
    .navigationTitle(Text(.settings(.offlineTitle)))
    .navigationBarTitleDisplayMode(.inline)
    .confirmationDialog(
      String(localized: .settings(.clearOfflineConfirmTitle)),
      isPresented: $showClearConfirmation,
      titleVisibility: .visible
    ) {
      Button(String(localized: .settings(.clearOfflineConfirmButton)), role: .destructive) {
        offlineManager.clearOfflineDocuments()
      }
      Button(String(localized: .localizable(.cancel)), role: .cancel) {}
    } message: {
      Text(.settings(.clearOfflineConfirmMessage))
    }
  }

  @ViewBuilder
  private var downloadProgressView: some View {
    VStack(alignment: .leading, spacing: 8) {
      HStack {
        Text(.settings(.downloading))
        Spacer()
        Text("\(offlineManager.downloadState.downloadedDocuments)/\(offlineManager.downloadState.totalDocuments)")
          .foregroundStyle(.secondary)
          .contentTransition(.numericText())
      }

      ProgressView(value: offlineManager.downloadState.overallProgress)
        .progressViewStyle(.linear)
        .animation(.easeInOut(duration: 0.3), value: offlineManager.downloadState.overallProgress)

      HStack {
        // Progress percentage
        Text("\(offlineManager.downloadState.progressPercentage)%")
          .font(.caption)
          .foregroundStyle(.secondary)
          .contentTransition(.numericText())

        Spacer()

        // Download speed
        if offlineManager.downloadState.downloadSpeed > 0 {
          Text(OfflineManager.formatSpeed(offlineManager.downloadState.downloadSpeed))
            .font(.caption)
            .foregroundStyle(.secondary)
            .contentTransition(.numericText())
            .animation(.easeInOut(duration: 0.5), value: offlineManager.downloadState.downloadSpeed)
        }
      }

      // Downloaded size
      Text(OfflineManager.formatBytes(offlineManager.downloadState.bytesDownloaded))
        .font(.caption)
        .foregroundStyle(.secondary)
        .contentTransition(.numericText())

      Button {
        offlineManager.cancelDownload()
      } label: {
        Label {
          Text(.settings(.cancelDownload))
        } icon: {
          Image(systemName: "xmark.circle.fill")
        }
      }
      .foregroundStyle(.red)
    }
    .padding(.vertical, 4)
  }
}

#Preview {
  NavigationStack {
    OfflineSettingsView()
      .environmentObject(OfflineManager())
  }
}
