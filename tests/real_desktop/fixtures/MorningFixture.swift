import AppKit
final class Demo: NSObject, NSApplicationDelegate {
    var window: NSWindow!
    let counter = NSTextField(labelWithString: "计数 0")
    var value = 0
    func applicationDidFinishLaunching(_ notification: Notification) {
        window = NSWindow(contentRect: NSRect(x: 0,y: 0,width: 430,height: 220), styleMask: [.titled,.closable,.miniaturizable], backing: .buffered, defer: false)
        window.title = "Morning 电脑操作验收"
        let content = window.contentView!
        let caption = NSTextField(labelWithString: "独立测试窗口 · 不含用户数据")
        caption.frame = NSRect(x: 36,y: 173,width: 370,height: 23)
        counter.frame = NSRect(x: 36,y: 112,width: 340,height: 43)
        counter.font = NSFont.systemFont(ofSize: 30,weight: .medium)
        let add = NSButton(title: "增加",target:self,action:#selector(increase))
        add.frame = NSRect(x: 30,y: 42,width: 165,height: 36);add.bezelStyle = .rounded
        let reset = NSButton(title: "清零",target:self,action:#selector(clear))
        reset.frame = NSRect(x: 216,y: 42,width: 165,height: 36);reset.bezelStyle = .rounded
        content.addSubview(caption);content.addSubview(counter);content.addSubview(add);content.addSubview(reset)
        window.center();window.makeKeyAndOrderFront(nil);NSApp.activate(ignoringOtherApps:true)
    }
    @objc func increase(){value += 1;counter.stringValue = "计数 \(value)"}
    @objc func clear(){value = 0;counter.stringValue = "计数 0"}
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {true}
}
let app = NSApplication.shared
let delegate = Demo()
app.setActivationPolicy(.regular);app.delegate = delegate;app.run()
