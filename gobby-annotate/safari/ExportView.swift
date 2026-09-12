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
                ShareLink(item: file) { Label("Share", systemImage: "square.and.arrow.up") }
              }.buttonStyle(.bordered)
            }.padding(.vertical, 4)
          }
        }
        Section {
          Button("Refresh exports", systemImage: "arrow.clockwise") { refresh() }
          Button("Clear interrupted transfers") {
            do {
              try ExportStore.shared().clearInterrupted()
              status = "Interrupted transfers cleared. Completed exports retained."
            } catch { self.error = error.localizedDescription }
          }
          if !status.isEmpty { Text(status).accessibilityAddTraits(.updatesFrequently) }
        }
      }
      .navigationTitle("Gobby Annotate")
      .tint(Color(red: 0.43, green: 0.55, blue: 0.13))
      .task { refresh() }
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
  private func refresh() {
    do { files = try ExportStore.shared().completed() } catch {
      self.error = error.localizedDescription
    }
  }
}
