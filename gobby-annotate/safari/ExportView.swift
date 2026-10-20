import SwiftUI
import UniformTypeIdentifiers

struct CaptureDocument: FileDocument {
  static var readableContentTypes: [UTType] { [.zip] }
  var bytes: Data
  init(bytes: Data) { self.bytes = bytes }
  init(configuration: ReadConfiguration) throws {
    guard let data = configuration.file.regularFileContents else {
      throw CocoaError(.fileReadCorruptFile)
    }
    bytes = data
  }
  func fileWrapper(configuration: WriteConfiguration) throws -> FileWrapper {
    FileWrapper(regularFileWithContents: bytes)
  }
}

struct ExportView: View {
  @Environment(\.scenePhase) private var scenePhase
  @State private var files: [URL] = []
  @State private var error: String?
  @State private var errorDetail = ""
  @State private var interrupted = 0
  @State private var document: CaptureDocument?
  @State private var name = "capture.zip"
  @State private var saving = false
  @State private var status = ""
  @State private var pendingDelete: URL?
  @State private var confirmingDeleteAll = false
  @State private var path: [URL] = []
  var body: some View {
    NavigationStack(path: $path) {
      List {
        Section {
          Text(
            "Enable Gobby Annotate in Safari Extensions, then use its toolbar action on the page you want to review."
          )
          Text(
            "Exports are staged on this device. Save or share a ZIP to your coding computer before importing it."
          )
        }
        Section {
          if files.isEmpty { Text("No exports yet. Add a note in Safari and choose Export ZIP.") }
          ForEach(files, id: \.self) { file in
            NavigationLink(value: file) {
              Text(file.lastPathComponent)
                .font(.caption.monospaced())
                .lineLimit(1)
                .truncationMode(.middle)
            }
            .swipeActions(edge: .trailing, allowsFullSwipe: false) {
              Button("Delete", systemImage: "trash", role: .destructive) {
                pendingDelete = file
              }
              ShareLink(item: file) {
                Label("Share", systemImage: "square.and.arrow.up")
              }
            }
            .contextMenu {
              Button("Export", systemImage: "square.and.arrow.down") { export(file) }
              ShareLink(item: file) {
                Label("Share", systemImage: "square.and.arrow.up")
              }
              Button("Delete", systemImage: "trash", role: .destructive) {
                pendingDelete = file
              }
            }
          }
          .onDelete { offsets in
            pendingDelete = offsets.first.map { files[$0] }
          }
        } header: {
          // Primary, not the default secondary: the secondary header fails the contrast audit.
          Text("Staged exports").foregroundStyle(.primary)
        }
        Section {
          Button("Refresh exports", systemImage: "arrow.clockwise") { refresh() }
          if !files.isEmpty {
            Button("Delete all staged exports", systemImage: "trash") {
              confirmingDeleteAll = true
            }
            .foregroundStyle(Color("Destructive"))
          }
        }
        Section {
          Text(interruptedStatus)
          Button("Clear interrupted transfers") {
            do {
              try ExportStore.shared().clearInterrupted()
              report("Interrupted transfers cleared. Completed exports retained.")
            } catch { fail("Interrupted transfers couldn't be cleared. Try again.", error) }
            refresh()
          }
        } header: {
          Text("Interrupted transfers").foregroundStyle(.primary)
        }
      }
      .navigationDestination(for: URL.self) { file in
        ExportDetail(
          file: file, onExport: { export($0) }, onDelete: { deleteExport($0) })
      }
      .navigationTitle("Gobby Annotate")
      .toolbar {
        #if os(iOS)
          if !files.isEmpty {
            ToolbarItem(placement: .topBarTrailing) { EditButton() }
          }
        #endif
      }
      .tint(Color.accentColor)
      .task { refresh() }
      .safeAreaInset(edge: .bottom) { statusBar }
      .confirmationDialog(
        "Delete this staged export?",
        isPresented: Binding(
          get: { pendingDelete != nil }, set: { if !$0 { pendingDelete = nil } }),
        titleVisibility: .visible, presenting: pendingDelete
      ) { file in
        Button("Delete export", role: .destructive) { deleteExport(file) }
        Button("Cancel", role: .cancel) {}
      } message: { file in
        Text(singleDeleteMessage(file.lastPathComponent))
      }
      .confirmationDialog(
        files.count == 1
          ? "Delete the staged export?" : "Delete all \(files.count) staged exports?",
        isPresented: $confirmingDeleteAll, titleVisibility: .visible
      ) {
        Button(
          files.count == 1 ? "Delete 1 export" : "Delete \(files.count) exports", role: .destructive
        ) {
          remove(
            "Staged exports deleted. Safari removes their batches when Gobby Annotate next opens there. Unfinished transfers retained."
          ) {
            try $0.deleteCompleted()
          }
        }
        Button("Cancel", role: .cancel) {}
      } message: {
        Text(
          "This removes the ZIP files from this iPhone. Gobby Annotate in Safari removes each exported batch the next time it opens there. Drafts with unexported changes and unfinished transfers stay."
        )
      }
      .onChange(of: scenePhase) { _, phase in if phase == .active { refresh() } }
    }
    .frame(minWidth: 280, minHeight: 300)
    // Presented from the stack itself, so the save sheet and error alert still show over a
    // pushed detail view.
    .fileExporter(
      isPresented: $saving, document: document, contentType: .zip, defaultFilename: name
    ) { result in
      switch result {
      case .success: report("Export saved. Staged copy retained.")
      case .failure(let failure):
        fail(
          "The ZIP couldn't be saved to that location. Choose another location and try again.",
          failure)
      }
    }
    .alert(
      "Export unavailable",
      isPresented: Binding(get: { error != nil }, set: { if !$0 { error = nil } })
    ) {
      Button("OK") { error = nil }
    } message: {
      Text(error ?? "")
        + Text(errorDetail.isEmpty ? "" : "\n\(errorDetail)").foregroundStyle(.secondary)
    }
  }
  private func export(_ file: URL) {
    do {
      document = CaptureDocument(bytes: try Data(contentsOf: file))
      name = file.lastPathComponent
      saving = true
    } catch {
      fail("The staged ZIP couldn't be read. Refresh exports and try again.", error)
    }
  }
  @ViewBuilder private var statusBar: some View {
    VStack(spacing: 0) {
      if !status.isEmpty {
        Label(status, systemImage: "checkmark.circle")
          .font(.footnote)
          .foregroundStyle(.secondary)
          .frame(maxWidth: .infinity, alignment: .leading)
          .padding(.horizontal)
          .padding(.vertical, 12)
          .background(.bar)
          .accessibilityAddTraits(.updatesFrequently)
      }
    }
  }
  private func report(_ message: String) {
    status = message
    AccessibilityNotification.Announcement(message).post()
  }
  private func deleteExport(_ file: URL) {
    remove(
      "Export deleted from this iPhone. Safari removes its batch when Gobby Annotate next opens there."
    ) { try $0.delete(file) }
  }
  private func remove(_ done: String, _ work: (ExportStore) throws -> Void) {
    do {
      try work(ExportStore.shared())
      path = []
      report(done)
    } catch {
      fail("The export couldn't be deleted. Refresh exports to see what is left.", error)
    }
    refresh()
  }
  private func refresh() {
    do {
      files = try ExportStore.shared().completed()
      interrupted = try ExportStore.shared().interruptedCount()
    } catch {
      fail("Staged exports couldn't be loaded. Tap Refresh exports to try again.", error)
    }
  }
  /// Shows the plain-language message first; the system error text follows in secondary style.
  private func fail(_ message: String, _ cause: Error) {
    error = message
    errorDetail = cause.localizedDescription
  }
  private var interruptedStatus: String {
    if interrupted == 0 { return "No exports are waiting to finish copying." }
    let noun = interrupted == 1 ? "export stopped" : "exports stopped"
    return "\(interrupted) \(noun) before copying finished. "
      + "Clear them, then export the batch again from Safari."
  }
}

/// Copy for removing one staged export; the list and the detail view both confirm with it.
private func singleDeleteMessage(_ name: String) -> String {
  "\(name) is removed from this iPhone. Gobby Annotate in Safari removes that batch, and any other ZIPs staged for it, the next time it opens there. Drafts with unexported changes stay."
}

/// Tap target for a staged export. Export, Share and Delete are plain buttons, so none of them
/// needs a gesture or a menu.
private struct ExportDetail: View {
  let file: URL
  let onExport: (URL) -> Void
  let onDelete: (URL) -> Void
  @State private var confirmingDelete = false
  var body: some View {
    List {
      Section {
        Text(file.lastPathComponent)
          .font(.caption.monospaced())
          .textSelection(.enabled)
      }
      Section {
        Button("Export", systemImage: "square.and.arrow.down") { onExport(file) }
        ShareLink(item: file) {
          Label("Share", systemImage: "square.and.arrow.up")
        }
      }
      Section {
        Button("Delete", systemImage: "trash", role: .destructive) { confirmingDelete = true }
      }
    }
    .navigationTitle("Staged export")
    .confirmationDialog(
      "Delete this staged export?", isPresented: $confirmingDelete, titleVisibility: .visible
    ) {
      Button("Delete export", role: .destructive) { onDelete(file) }
      Button("Cancel", role: .cancel) {}
    } message: {
      Text(singleDeleteMessage(file.lastPathComponent))
    }
  }
}
