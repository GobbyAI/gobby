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
  @State private var document: CaptureDocument?
  @State private var name = "capture.zip"
  @State private var saving = false
  @State private var status = ""
  @State private var pendingDelete: URL?
  @State private var confirmingDeleteAll = false
  var body: some View {
    NavigationStack {
      List {
        Section {
          Text(
            "Enable Gobby Annotate in Safari Extensions, then use its toolbar action on the page you want to review."
          )
          Text(
            "Exports are staged on this device. Save or share a ZIP to your coding computer before importing it."
          )
        }
        Section("Staged exports") {
          if files.isEmpty { Text("No exports yet. Add a note in Safari and choose Export ZIP.") }
          ForEach(files, id: \.self) { file in
            VStack(alignment: .leading, spacing: 8) {
              Text(file.lastPathComponent).font(.caption.monospaced()).textSelection(.enabled)
              HStack {
                Button("Save", systemImage: "square.and.arrow.down") {
                  do {
                    document = CaptureDocument(bytes: try Data(contentsOf: file))
                    name = file.lastPathComponent
                    saving = true
                  } catch { self.error = error.localizedDescription }
                }
                .lineLimit(1)
                .fixedSize(horizontal: true, vertical: false)
                .frame(minHeight: 44)
                ShareLink(item: file) { Label("Share", systemImage: "square.and.arrow.up") }
                  .lineLimit(1)
                  .fixedSize(horizontal: true, vertical: false)
                  .frame(minHeight: 44)
                Button("Delete", systemImage: "trash") { pendingDelete = file }
                  .tint(Color("Destructive"))
                  .accessibilityLabel("Delete \(file.lastPathComponent)")
                  .lineLimit(1)
                  .fixedSize(horizontal: true, vertical: false)
                  .frame(minHeight: 44)
              }.buttonStyle(.bordered)
            }.padding(.vertical, 4)
          }
        }
        Section {
          Button("Refresh exports", systemImage: "arrow.clockwise") { refresh() }
          if !files.isEmpty {
            Button("Delete all staged exports", systemImage: "trash") {
              confirmingDeleteAll = true
            }
            .foregroundStyle(Color("Destructive"))
          }
          Button("Clear interrupted transfers") {
            do {
              try ExportStore.shared().clearInterrupted()
              status = "Interrupted transfers cleared. Completed exports retained."
            } catch { self.error = error.localizedDescription }
          }
        }
      }
      .navigationTitle("Gobby Annotate")
      .tint(Color.accentColor)
      .task { refresh() }
      .safeAreaInset(edge: .bottom) {
        if !status.isEmpty {
          Label(status, systemImage: "checkmark.circle")
            .font(.footnote)
            .foregroundStyle(.secondary)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal)
            .padding(.vertical, 12)
            .background(.bar)
            .accessibilityAddTraits(.isStatusElement)
        }
      }
      .confirmationDialog(
        "Delete this staged export?",
        isPresented: Binding(
          get: { pendingDelete != nil }, set: { if !$0 { pendingDelete = nil } }),
        titleVisibility: .visible, presenting: pendingDelete
      ) { file in
        Button("Delete export", role: .destructive) {
          remove("Export deleted from this iPhone.") { try $0.delete(file) }
        }
        Button("Cancel", role: .cancel) {}
      } message: { file in
        Text(
          "\(file.lastPathComponent) is removed from this iPhone. Delete the batch in Safari to remove its notes there too."
        )
      }
      .confirmationDialog(
        files.count == 1
          ? "Delete the staged export?" : "Delete all \(files.count) staged exports?",
        isPresented: $confirmingDeleteAll, titleVisibility: .visible
      ) {
        Button(
          files.count == 1 ? "Delete 1 export" : "Delete \(files.count) exports", role: .destructive
        ) {
          remove("Staged exports deleted. Unfinished transfers retained.") {
            try $0.deleteCompleted()
          }
        }
        Button("Cancel", role: .cancel) {}
      } message: {
        Text(
          "This removes the ZIP files from this iPhone. Batches still open in Safari are not affected."
        )
      }
      .onChange(of: scenePhase) { _, phase in if phase == .active { refresh() } }
      .fileExporter(
        isPresented: $saving, document: document, contentType: .zip, defaultFilename: name
      ) { result in
        switch result {
        case .success: status = "Export saved. Staged copy retained."
        case .failure(let failure): error = failure.localizedDescription
        }
      }
      .alert(
        "Export unavailable",
        isPresented: Binding(get: { error != nil }, set: { if !$0 { error = nil } })
      ) {
        Button("OK") { error = nil }
      } message: {
        Text(error ?? "")
      }
    }
    .frame(minWidth: 280, minHeight: 300)
  }
  private func remove(_ done: String, _ work: (ExportStore) throws -> Void) {
    do {
      try work(ExportStore.shared())
      status = done
    } catch { self.error = error.localizedDescription }
    refresh()
  }
  private func refresh() {
    do { files = try ExportStore.shared().completed() } catch {
      self.error = error.localizedDescription
    }
  }
}
