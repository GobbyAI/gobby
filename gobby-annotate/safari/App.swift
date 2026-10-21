import SwiftUI

@main
struct GobbyAnnotateApp: App {
  var body: some Scene {
    WindowGroup { ExportView().preferredColorScheme(Self.uiTestColorScheme) }
  }

  /// UI-test seam: `-GobbyAnnotateColorScheme dark` or `light` pins the scheme before the first
  /// frame. XCUIDevice.appearance set before launch rendered light on the iOS 26.5 runtime.
  /// Debug builds only; release builds ignore the argument.
  private static var uiTestColorScheme: ColorScheme? {
    #if DEBUG
      switch UserDefaults.standard.string(forKey: "GobbyAnnotateColorScheme") {
      case "dark": return .dark
      case "light": return .light
      default: return nil
      }
    #else
      return nil
    #endif
  }
}
