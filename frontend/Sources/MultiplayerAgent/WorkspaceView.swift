import SwiftUI
import AppKit

private enum Palette {
    static let background = Color(red: 0.055, green: 0.067, blue: 0.09)
    static let panel = Color(red: 0.09, green: 0.105, blue: 0.14)
    static let panelRaised = Color(red: 0.12, green: 0.14, blue: 0.18)
    static let line = Color.white.opacity(0.09)
    static let muted = Color(red: 0.57, green: 0.61, blue: 0.69)
    static let mint = Color(red: 0.43, green: 0.92, blue: 0.73)
    static let violet = Color(red: 0.66, green: 0.59, blue: 1.0)
}

struct WorkspaceView: View {
    @EnvironmentObject private var model: RoomModel
    @State private var taskText = ""
    @State private var steeringText = ""
    @State private var selectedNode: String?
    @State private var selectedSnapshot: [String: Any]?
    @State private var roomCode = ""
    @State private var showGraphExplorer = false
    @State private var showMembersPopover = false
    @State private var authEmail = ""
    @State private var authPassword = ""
    @State private var authName = ""
    @State private var creatingAccount = false

    private var nodes: [[String: Any]] { model.graph["nodes"] as? [[String: Any]] ?? [] }
    private var head: String? { model.graph["head_id"] as? String }

    var body: some View {
        Group {
            if model.isAuthenticated {
                HStack(spacing: 0) {
                    sidebar
                    Rectangle().fill(Palette.line).frame(width: 1)
                    VStack(spacing: 0) {
                        topBar
                        if model.selectedRoom == nil { welcome }
                        else { dashboard }
                    }
                }
            } else {
                authentication
            }
        }
        .background(Palette.background)
        .onAppear {
            // Activate after SwiftUI has installed the WindowGroup window.
            DispatchQueue.main.async {
                NSApplication.shared.setActivationPolicy(.regular)
                NSApplication.shared.activate(ignoringOtherApps: true)
            }
        }
        .task { await model.refreshRooms() }
        .alert("Something went wrong", isPresented: Binding(
            get: { model.errorMessage != nil }, set: { if !$0 { model.errorMessage = nil } })) {
            Button("OK") { model.errorMessage = nil }
        } message: { Text(model.errorMessage ?? "") }
    }

    private var authentication: some View {
        VStack(spacing: 18) {
            HStack(spacing: 11) {
                Image(systemName: "point.3.connected.trianglepath.dotted")
                    .foregroundStyle(Palette.mint).frame(width: 42, height: 42)
                    .background(Palette.mint.opacity(0.16)).clipShape(RoundedRectangle(cornerRadius: 12))
                VStack(alignment: .leading, spacing: 3) {
                    Text("MULTIPLAYER").font(.system(size: 14, weight: .black, design: .rounded)).tracking(2)
                    Text("SHARED AI AGENT WORKSPACES").font(.system(size: 8, weight: .bold)).tracking(1.2).foregroundStyle(Palette.muted)
                }
            }
            Text(creatingAccount ? "Create your account" : "Sign in")
                .font(.system(size: 23, weight: .bold, design: .rounded))
            if creatingAccount {
                TextField("Your name", text: $authName).textFieldStyle(.roundedBorder)
            }
            TextField("Email address", text: $authEmail).textFieldStyle(.roundedBorder)
                .textContentType(.username).autocorrectionDisabled()
            SecureField("Password (at least 10 characters)", text: $authPassword)
                .textFieldStyle(.roundedBorder).textContentType(creatingAccount ? .newPassword : .password)
            Button {
                Task { await model.authenticate(email: authEmail, password: authPassword,
                                                name: creatingAccount ? authName : nil) }
            } label: {
                Text(creatingAccount ? "Create account" : "Sign in")
                    .font(.system(size: 12, weight: .bold)).frame(maxWidth: .infinity)
                    .padding(.vertical, 11).background(Palette.mint).foregroundStyle(Palette.background)
                    .clipShape(RoundedRectangle(cornerRadius: 9))
            }.buttonStyle(.plain)
            Button(creatingAccount ? "Already have an account? Sign in" : "Create an account") {
                creatingAccount.toggle()
            }.buttonStyle(.plain).foregroundStyle(Palette.mint)
        }
        .padding(26).frame(width: 380).background(Palette.panel)
        .clipShape(RoundedRectangle(cornerRadius: 18))
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private var sidebar: some View {
        VStack(alignment: .leading, spacing: 22) {
            HStack(spacing: 11) {
                ZStack {
                    RoundedRectangle(cornerRadius: 11).fill(Palette.mint.opacity(0.16)).frame(width: 38, height: 38)
                    Image(systemName: "point.3.connected.trianglepath.dotted").foregroundStyle(Palette.mint)
                }
                VStack(alignment: .leading, spacing: 2) {
                    Text("MULTIPLAYER").font(.system(size: 13, weight: .black, design: .rounded)).tracking(2)
                    Text("AI AGENT WORKSPACE").font(.system(size: 8, weight: .bold)).tracking(1.4).foregroundStyle(Palette.muted)
                }
            }
            Button { Task { await model.createRoom() } } label: {
                Label("New workspace", systemImage: "plus")
                    .font(.system(size: 12, weight: .semibold)).frame(maxWidth: .infinity)
                    .padding(.vertical, 11).background(Palette.mint).foregroundStyle(Palette.background)
                    .clipShape(RoundedRectangle(cornerRadius: 10))
            }.buttonStyle(.plain)
            HStack(spacing: 6) {
                TextField("Paste room ID", text: $roomCode)
                    .textFieldStyle(.plain).font(.system(size: 10, design: .monospaced))
                    .padding(.horizontal, 9).padding(.vertical, 9).background(Palette.panel)
                    .clipShape(RoundedRectangle(cornerRadius: 8))
                    .onSubmit(joinRoom)
                Button(action: joinRoom) {
                    Image(systemName: "arrow.turn.down.right").font(.system(size: 10, weight: .bold))
                        .foregroundStyle(Palette.mint).frame(width: 31, height: 31).background(Palette.panel)
                        .clipShape(RoundedRectangle(cornerRadius: 8))
                }.buttonStyle(.plain).help("Join a shared room by ID")
            }

            VStack(alignment: .leading, spacing: 9) {
                Text("WORKSPACES").font(.system(size: 9, weight: .bold)).tracking(1.5).foregroundStyle(Palette.muted)
                if model.rooms.isEmpty {
                    Text("Your shared rooms will appear here.").font(.system(size: 11)).foregroundStyle(Palette.muted).padding(.vertical, 8)
                }
                ForEach(model.rooms) { room in
                    Button { Task { await model.connect(to: room) } } label: {
                        HStack(spacing: 10) {
                            Circle().fill(model.selectedRoom?.id == room.id ? Palette.mint : Palette.muted.opacity(0.5)).frame(width: 7, height: 7)
                            VStack(alignment: .leading, spacing: 3) {
                                Text(room.name).font(.system(size: 12, weight: .medium)).lineLimit(1)
                                Text(String(room.id.prefix(12))).font(.system(size: 9, design: .monospaced)).foregroundStyle(Palette.muted)
                            }
                            Spacer(minLength: 0)
                        }.padding(9).background(model.selectedRoom?.id == room.id ? Palette.panelRaised : .clear)
                            .clipShape(RoundedRectangle(cornerRadius: 9))
                    }.buttonStyle(.plain)
                }
            }
            Spacer()
            Button { Task { await model.signOut() } } label: {
                Label("Sign out", systemImage: "rectangle.portrait.and.arrow.right")
                    .font(.system(size: 10, weight: .medium)).foregroundStyle(Palette.muted)
            }.buttonStyle(.plain)
            HStack(spacing: 8) {
                Circle().fill(model.connectionState == "Connected" ? Palette.mint : .orange).frame(width: 7, height: 7)
                Text(model.connectionState).font(.system(size: 10, weight: .medium)).foregroundStyle(Palette.muted)
            }.padding(.bottom, 8)
        }
        .padding(18).frame(width: 230).background(Palette.background)
    }

    private var topBar: some View {
        HStack {
            VStack(alignment: .leading, spacing: 4) {
                Text(model.selectedRoom?.name ?? "Your agent workspace")
                    .font(.system(size: 18, weight: .semibold, design: .rounded))
                Text(model.selectedRoom.map { "ROOM / \($0.id)" } ?? "A shared place to watch, guide, and branch agent runs")
                    .font(.system(size: 9, weight: .medium, design: .monospaced)).tracking(0.5).foregroundStyle(Palette.muted)
            }
            Spacer()
            if model.selectedRoom != nil {
                Button { model.copyInvite() } label: {
                    Label("Copy invite", systemImage: "link")
                        .font(.system(size: 9, weight: .semibold)).foregroundStyle(Palette.muted)
                        .padding(.horizontal, 9).padding(.vertical, 7).background(Palette.panel).clipShape(Capsule())
                }.buttonStyle(.plain).help("Copy a link teammates can open to join")
                HStack(spacing: 7) {
                    Circle().fill(stateColor).frame(width: 7, height: 7)
                    Text(model.runState.uppercased()).font(.system(size: 9, weight: .bold)).tracking(1)
                }.padding(.horizontal, 10).padding(.vertical, 7).background(Palette.panel).clipShape(Capsule())
                Button { showMembersPopover.toggle() } label: {
                    HStack(spacing: 5) {
                        Image(systemName: "person.2.fill").foregroundStyle(Palette.violet)
                        Text("\(model.members.count)").foregroundStyle(Palette.muted)
                    }.font(.system(size: 10, weight: .semibold)).padding(.horizontal, 10).padding(.vertical, 7).background(Palette.panel).clipShape(Capsule())
                }.buttonStyle(.plain).help("See who is connected to this room")
                    .popover(isPresented: $showMembersPopover, arrowEdge: .bottom) {
                        VStack(alignment: .leading, spacing: 12) {
                            Text("Room members").font(.system(size: 14, weight: .semibold, design: .rounded))
                            Text("\(model.members.count) connected")
                                .font(.system(size: 10)).foregroundStyle(Palette.muted)
                            if model.members.isEmpty {
                                Text("No one is connected yet.").font(.system(size: 11)).foregroundStyle(Palette.muted)
                            } else {
                                ForEach(Array(model.members.enumerated()), id: \.offset) { _, member in
                                    HStack(spacing: 8) {
                                        Circle().fill(Palette.mint).frame(width: 7, height: 7)
                                        Text(member["name"] as? String ?? "Teammate")
                                            .font(.system(size: 11, weight: .medium))
                                        if member["id"] as? String == model.userID {
                                            Text("YOU").font(.system(size: 8, weight: .bold)).foregroundStyle(Palette.violet)
                                        }
                                        Spacer()
                                        Text("Connected").font(.system(size: 9)).foregroundStyle(Palette.muted)
                                    }
                                }
                            }
                        }
                        .padding(16).frame(width: 240).background(Palette.panel)
                    }
            }
            Button { Task { await model.refreshRooms() } } label: {
                Image(systemName: "arrow.clockwise").font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(Palette.muted).frame(width: 31, height: 31).background(Palette.panel).clipShape(Circle())
            }.buttonStyle(.plain)
        }
        .padding(.horizontal, 24).padding(.vertical, 15).background(Palette.background)
        .overlay(alignment: .bottom) { Rectangle().fill(Palette.line).frame(height: 1) }
    }

    private var dashboard: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                runComposer
                if model.finalResult != nil || !model.executionTranscript.isEmpty { executionResultPanel }
                if !model.approvalGates.isEmpty { approvalPanel }
                HStack(alignment: .top, spacing: 16) {
                    VStack(spacing: 16) {
                        statePanel
                        steeringPanel
                    }.frame(maxWidth: .infinity)
                    activityPanel.frame(width: 330)
                }
            }.padding(22)
        }
    }

    private var runComposer: some View {
        VStack(alignment: .leading, spacing: 13) {
            HStack {
                Label("START A RUN", systemImage: "sparkles").font(.system(size: 9, weight: .bold)).tracking(1.3).foregroundStyle(Palette.mint)
                Spacer()
                Text(model.connectionState.uppercased()).font(.system(size: 8, weight: .bold, design: .monospaced)).foregroundStyle(Palette.muted)
            }
            HStack(alignment: .bottom, spacing: 12) {
                TextField("Describe a task for the shared agent…", text: $taskText, axis: .vertical)
                    .lineLimit(2...4).textFieldStyle(.plain).font(.system(size: 13)).padding(12)
                    .background(Palette.background).clipShape(RoundedRectangle(cornerRadius: 10))
                    .onSubmit(startRun)
                Button(action: startRun) {
                    HStack(spacing: 7) { Image(systemName: "play.fill"); Text("Run agent") }
                        .font(.system(size: 11, weight: .bold)).padding(.horizontal, 15).padding(.vertical, 12)
                        .background(Palette.mint).foregroundStyle(Palette.background).clipShape(RoundedRectangle(cornerRadius: 9))
                }.buttonStyle(.plain).disabled(taskText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
        }.panel()
    }

    private var executionResultPanel: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Agent result").font(.system(size: 14, weight: .semibold, design: .rounded))
                    Text(resultStatus)
                        .font(.system(size: 8, weight: .bold)).tracking(1).foregroundStyle(Palette.muted)
                }
                Spacer()
                Button {
                    model.copyExecutionResult()
                } label: {
                    Label("Copy result", systemImage: "doc.on.doc")
                        .font(.system(size: 9, weight: .semibold)).foregroundStyle(Palette.mint)
                }.buttonStyle(.plain)
            }
            if let result = model.finalResult {
                ScrollView {
                    Text(result).font(.system(size: 12)).foregroundStyle(.white.opacity(0.9))
                        .frame(maxWidth: .infinity, alignment: .leading).textSelection(.enabled)
                        .padding(12)
                }.frame(maxHeight: 220).background(Palette.background)
                    .clipShape(RoundedRectangle(cornerRadius: 9))
            } else {
                Text(model.runState == "failed" ? "The run failed. Expand the transcript below for its error details." :
                     model.runState == "interrupted" ? "The run was interrupted before it produced a final answer." :
                     "The agent is still working. Its final answer will appear here when the run finishes.")
                    .font(.system(size: 10)).foregroundStyle(Palette.muted)
            }
            DisclosureGroup("Full execution transcript") {
                ScrollView {
                    Text(model.executionTranscript).font(.system(size: 10, design: .monospaced))
                        .foregroundStyle(.white.opacity(0.78)).frame(maxWidth: .infinity, alignment: .leading)
                        .textSelection(.enabled).padding(10)
                }.frame(maxHeight: 320).background(Palette.background)
                    .clipShape(RoundedRectangle(cornerRadius: 8)).padding(.top, 6)
            }.font(.system(size: 10, weight: .semibold)).foregroundStyle(Palette.muted)
        }.panel()
    }

    private var resultStatus: String {
        switch model.runState {
        case "failed": "RUN FAILED · ERROR DETAILS BELOW"
        case "interrupted": "RUN INTERRUPTED · PARTIAL ACTIVITY"
        default: model.finalResult == nil ? "RUN IN PROGRESS · FULL ACTIVITY" : "COMPLETE OUTPUT · FULL ACTIVITY"
        }
    }

    private var approvalPanel: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Human approval required").font(.system(size: 14, weight: .semibold, design: .rounded))
            ForEach(Array(model.approvalGates.enumerated()), id: \.offset) { _, gate in
                let action = gate["action"] as? [String: Any] ?? [:]
                let gateID = gate["gate_id"] as? String ?? ""
                let eligibleVoters = gate["eligible_voters"] as? [String] ?? []
                let canVote = eligibleVoters.contains(model.userID)
                VStack(alignment: .leading, spacing: 8) {
                    Text("\(action["tool"] as? String ?? "Action") · \(gate["votes"].flatMap { $0 as? [[String: Any]] }?.filter { ($0["approve"] as? Bool) == true }.count ?? 0)/\(gate["required"] as? Int ?? 1) approvals")
                        .font(.system(size: 11, weight: .semibold))
                    Text(String(describing: action["args"] ?? [:])).font(.system(size: 10, design: .monospaced))
                        .foregroundStyle(Palette.muted).textSelection(.enabled)
                    if canVote {
                        HStack {
                            Button("Approve") { Task { await model.vote(gateID: gateID, approve: true) } }.tint(Palette.mint)
                            Button("Reject") { Task { await model.vote(gateID: gateID, approve: false) } }.tint(.pink)
                        }.buttonStyle(.borderless)
                    } else {
                        Text("Waiting for an eligible reviewer to decide.")
                            .font(.system(size: 10)).foregroundStyle(Palette.muted)
                    }
                }.padding(11).background(Palette.background).clipShape(RoundedRectangle(cornerRadius: 9))
            }
            Text("Solo rooms allow the run starter to approve. In group rooms, the starter is excluded and quorum adjusts to connected reviewers.")
                .font(.system(size: 9)).foregroundStyle(Palette.muted)
        }.panel()
    }

    private var statePanel: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Execution graph").font(.system(size: 14, weight: .semibold, design: .rounded))
                    Text("CHECKPOINTS · BRANCHES · TIME TRAVEL").font(.system(size: 8, weight: .bold)).tracking(1).foregroundStyle(Palette.muted)
                }
                Spacer()
                Text("\(nodes.count) NODES").font(.system(size: 9, weight: .bold, design: .monospaced)).foregroundStyle(Palette.muted)
                Button { showGraphExplorer = true } label: {
                    Image(systemName: "arrow.up.left.and.arrow.down.right")
                        .font(.system(size: 10, weight: .semibold)).foregroundStyle(Palette.muted)
                        .frame(width: 28, height: 28).background(Palette.panelRaised).clipShape(Circle())
                }.buttonStyle(.plain).help("Open the full execution graph")
            }
            if nodes.isEmpty {
                ContentUnavailableView("No checkpoints yet", systemImage: "point.3.connected.trianglepath.dotted",
                                       description: Text("Start an agent run to build a versioned state graph."))
                    .frame(height: 150)
            } else {
                StateGraphCanvas(nodes: nodes, head: head) { id in
                    selectedNode = id
                    Task { selectedSnapshot = try? await model.call("state.scrub", args: ["node_id": id]) }
                }
                if let inspected = selectedSnapshot,
                   let snap = inspected["snapshot"] as? [String: Any],
                   let memory = snap["memory"] as? [[String: Any]] {
                    Divider().overlay(Palette.line)
                    HStack {
                        Text("CHECKPOINT INSPECTOR").font(.system(size: 8, weight: .bold)).tracking(1).foregroundStyle(Palette.muted)
                        Spacer()
                        HStack(spacing: 12) {
                            Button("Fork from here") {
                                let label = "Branch \(Int.random(in: 10...99))"
                                Task {
                                    do {
                                        _ = try await model.call("state.branch", args: ["node_id": snap["node_id"] ?? "", "name": label])
                                        await model.refreshGraph()
                                        selectedSnapshot = nil
                                    } catch { model.errorMessage = error.localizedDescription }
                                }
                            }.font(.system(size: 9, weight: .semibold)).buttonStyle(.plain).foregroundStyle(Palette.violet)
                            Button("Merge with head") {
                                guard let selectedNode, let head, selectedNode != head else { return }
                                Task {
                                    do {
                                        _ = try await model.call("state.merge", args: ["a": selectedNode, "b": head])
                                        await model.refreshGraph()
                                        selectedSnapshot = nil
                                    } catch { model.errorMessage = error.localizedDescription }
                                }
                            }.font(.system(size: 9, weight: .semibold)).buttonStyle(.plain).foregroundStyle(Palette.mint)
                        }
                    }
                    ForEach(Array(memory.suffix(3).enumerated()), id: \.offset) { _, entry in
                        HStack(alignment: .top, spacing: 8) {
                            Text((entry["kind"] as? String ?? "memory").uppercased()).font(.system(size: 8, weight: .bold, design: .monospaced)).foregroundStyle(Palette.violet).frame(width: 65, alignment: .leading)
                            Text(entry["text"] as? String ?? "").font(.system(size: 10)).foregroundStyle(.white.opacity(0.78)).lineLimit(2)
                        }
                    }
                }
            }
        }.panel()
            .sheet(isPresented: $showGraphExplorer) {
                VStack(alignment: .leading, spacing: 12) {
                    HStack {
                        VStack(alignment: .leading, spacing: 4) {
                            Text("Execution graph").font(.system(size: 18, weight: .semibold, design: .rounded))
                            Text("\(nodes.count) checkpoints · Click a node to inspect its saved state")
                                .font(.system(size: 10)).foregroundStyle(Palette.muted)
                        }
                        Spacer()
                        Button("Done") { showGraphExplorer = false }.buttonStyle(.bordered)
                    }
                    StateGraphCanvas(nodes: nodes, head: head, expanded: true) { id in
                        selectedNode = id
                        Task { selectedSnapshot = try? await model.call("state.scrub", args: ["node_id": id]) }
                    }
                    if let inspected = selectedSnapshot,
                       let node = inspected["node"] as? [String: Any],
                       let snapshot = inspected["snapshot"] as? [String: Any] {
                        Divider().overlay(Palette.line)
                        Text(node["label"] as? String ?? "Checkpoint")
                            .font(.system(size: 12, weight: .semibold))
                        if let memory = snapshot["memory"] as? [[String: Any]] {
                            ScrollView {
                                VStack(alignment: .leading, spacing: 7) {
                                    ForEach(Array(memory.suffix(8).enumerated()), id: \.offset) { _, entry in
                                        Text("\((entry["kind"] as? String ?? "memory").uppercased()) · \(entry["text"] as? String ?? "")")
                                            .font(.system(size: 10)).foregroundStyle(.white.opacity(0.78))
                                            .textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
                                    }
                                }
                            }.frame(maxHeight: 150)
                        }
                    }
                }
                .padding(20).frame(minWidth: 900, minHeight: 650)
                .background(Palette.background).preferredColorScheme(.dark)
            }
    }

    private var steeringPanel: some View {
        VStack(alignment: .leading, spacing: 13) {
            HStack {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Live controls").font(.system(size: 14, weight: .semibold, design: .rounded))
                    Text("ONE STEERING TOKEN · SHARED CONTROL").font(.system(size: 8, weight: .bold)).tracking(1).foregroundStyle(Palette.muted)
                }
                Spacer()
                if let holder = model.steeringHolder {
                    Label(holder, systemImage: "key.fill").font(.system(size: 9, weight: .medium)).foregroundStyle(Palette.mint)
                } else {
                    Text("UNCLAIMED").font(.system(size: 8, weight: .bold)).foregroundStyle(Palette.muted)
                }
            }
            HStack(spacing: 8) {
                ControlButton(title: "Pause", icon: "pause.fill", tint: .orange) { await model.steer("pause") }
                ControlButton(title: "Resume", icon: "play.fill", tint: Palette.mint) { await model.steer("resume") }
                ControlButton(title: "Interrupt", icon: "stop.fill", tint: .pink) { await model.steer("interrupt") }
                if model.steeringToken == nil {
                    ControlButton(title: "Take control", icon: "key.fill", tint: Palette.violet) { await model.acquireSteering() }
                } else {
                    Menu {
                        ForEach(Array(model.members.filter { ($0["id"] as? String) != nil }.enumerated()), id: \.offset) { _, member in
                            let memberID = member["id"] as? String ?? ""
                            if !memberID.isEmpty && memberID != model.userID {
                                Button(member["name"] as? String ?? "Teammate") {
                                    Task { await model.handoffSteering(to: memberID) }
                                }
                            }
                        }
                        Divider()
                        Button("Release control") { Task { await model.releaseSteering() } }
                    } label: {
                        Label("Transfer", systemImage: "arrowshape.turn.up.right.fill")
                            .font(.system(size: 9, weight: .semibold)).padding(.horizontal, 9).padding(.vertical, 8)
                            .background(Palette.violet.opacity(0.12)).foregroundStyle(Palette.violet)
                            .clipShape(RoundedRectangle(cornerRadius: 7))
                    }.menuStyle(.borderlessButton)
                }
            }
            HStack(spacing: 8) {
                TextField("Guide the next agent step…", text: $steeringText)
                    .textFieldStyle(.plain).font(.system(size: 11)).padding(10)
                    .background(Palette.background).clipShape(RoundedRectangle(cornerRadius: 8))
                    .onSubmit { inject() }
                Button(action: inject) {
                    Image(systemName: "arrow.up").font(.system(size: 10, weight: .bold)).foregroundStyle(Palette.background)
                        .frame(width: 31, height: 31).background(Palette.mint).clipShape(Circle())
                }.buttonStyle(.plain).disabled(steeringText.isEmpty)
            }
            if model.steeringToken == nil {
                Text("Claim the steering token before changing the agent run.").font(.system(size: 9)).foregroundStyle(Palette.muted)
            }
        }.panel()
    }

    private var activityPanel: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Live activity").font(.system(size: 14, weight: .semibold, design: .rounded))
                    Text("STREAMING FROM THIS ROOM").font(.system(size: 8, weight: .bold)).tracking(1).foregroundStyle(Palette.muted)
                }
                Spacer()
                Circle().fill(Palette.mint).frame(width: 6, height: 6).shadow(color: Palette.mint, radius: 5)
            }
            if model.events.isEmpty {
                ContentUnavailableView("Waiting for activity", systemImage: "waveform.path", description: Text("Agent thoughts and tool calls appear here."))
                    .frame(height: 190)
            } else {
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 11) {
                        ForEach(model.events) { event in
                            HStack(alignment: .top, spacing: 9) {
                                RoundedRectangle(cornerRadius: 2).fill(eventColor(event.type)).frame(width: 3)
                                VStack(alignment: .leading, spacing: 5) {
                                    HStack {
                                        Text(event.type.replacingOccurrences(of: "agent.", with: "").uppercased())
                                            .font(.system(size: 8, weight: .bold, design: .monospaced)).tracking(0.6)
                                        Spacer()
                                        Text(event.receivedAt, style: .time).font(.system(size: 8, design: .monospaced)).foregroundStyle(Palette.muted)
                                    }.foregroundStyle(eventColor(event.type))
                                    Text(eventSummary(event)).font(.system(size: 10)).foregroundStyle(.white.opacity(0.78)).lineLimit(5)
                                }
                            }.fixedSize(horizontal: false, vertical: true)
                        }
                    }
                }.frame(maxHeight: 600)
            }
            Divider().overlay(Palette.line)
            HStack(spacing: 6) {
                ForEach(Array(model.members.enumerated()), id: \.offset) { _, member in
                    Text(String((member["name"] as? String ?? "?").prefix(1)).uppercased())
                        .font(.system(size: 9, weight: .bold)).foregroundStyle(Palette.background)
                        .frame(width: 23, height: 23).background(Palette.violet).clipShape(Circle())
                }
                Text("\(model.members.count) here").font(.system(size: 9)).foregroundStyle(Palette.muted)
            }
        }.panel()
    }

    private var welcome: some View {
        VStack(spacing: 18) {
            Image(systemName: "point.3.connected.trianglepath.dotted").font(.system(size: 42, weight: .light)).foregroundStyle(Palette.mint)
            Text("One agent. The whole team.").font(.system(size: 25, weight: .semibold, design: .rounded))
            Text("Create a shared room to stream execution, steer the run together,\nand explore alternate branches of agent state.")
                .font(.system(size: 12)).foregroundStyle(Palette.muted).multilineTextAlignment(.center)
            Button { Task { await model.createRoom() } } label: {
                Label("Create a workspace", systemImage: "plus").font(.system(size: 11, weight: .bold))
                    .padding(.horizontal, 16).padding(.vertical, 11).background(Palette.mint).foregroundStyle(Palette.background).clipShape(RoundedRectangle(cornerRadius: 9))
            }.buttonStyle(.plain)
        }.frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private var stateColor: Color {
        switch model.runState { case "running": Palette.mint; case "paused": .orange; case "failed", "interrupted": .pink; default: Palette.muted }
    }

    private func eventColor(_ type: String) -> Color {
        if type.contains("thought") { return Palette.violet }
        if type.contains("tool") { return .cyan }
        if type.contains("error") { return .pink }
        if type.contains("steering") { return .orange }
        return Palette.mint
    }

    private func eventSummary(_ event: AgentEvent) -> String {
        if let text = event.payload["text"] as? String { return text }
        if let message = event.payload["message"] as? String { return message }
        if let summary = event.payload["summary"] as? String { return summary }
        if let tool = event.payload["tool"] as? [String: Any] { return "\(tool["name"] ?? "tool") · \(tool["args"] ?? [:])" }
        if let result = event.payload["result"] { return "Result · \(String(describing: result))" }
        return event.payload["state"] as? String ?? event.type
    }

    private func startRun() {
        let task = taskText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !task.isEmpty else { return }
        taskText = ""
        Task { await model.start(task: task) }
    }

    private func inject() {
        let note = steeringText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !note.isEmpty else { return }
        steeringText = ""
        Task { await model.steer("inject", text: note) }
    }

    private func joinRoom() {
        let input = roomCode.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !input.isEmpty else { return }
        roomCode = ""
        let id: String
        let invite: String?
        if let url = URL(string: input), let components = URLComponents(url: url, resolvingAgainstBaseURL: false) {
            id = components.queryItems?.first(where: { $0.name == "room" })?.value
                ?? components.path.split(separator: "/").last.map(String.init) ?? input
            invite = components.queryItems?.first(where: { $0.name == "invite" })?.value
        } else { id = input; invite = nil }
        Task {
            do {
                var components = URLComponents(url: model.baseURL.appending(path: "rooms/\(id)"), resolvingAgainstBaseURL: false)!
                if let invite { components.queryItems = [URLQueryItem(name: "invite", value: invite)] }
                let (data, response) = try await URLSession.shared.data(from: components.url!)
                guard (response as? HTTPURLResponse)?.statusCode == 200 else {
                    throw NSError(domain: "MultiplayerRoom", code: (response as? HTTPURLResponse)?.statusCode ?? 0,
                                  userInfo: [NSLocalizedDescriptionKey: "Room could not be opened. Sign in and use a valid invite link."])
                }
                let room = try JSONDecoder().decode(RoomSummary.self, from: data)
                if !model.rooms.contains(where: { $0.id == room.id }) { model.rooms.insert(room, at: 0) }
                await model.connect(to: room)
            } catch { model.errorMessage = "Could not join room: \(error.localizedDescription)" }
        }
    }
}

private struct StateGraphCanvas: View {
    let nodes: [[String: Any]]
    let head: String?
    var expanded = false
    let onSelect: (String) -> Void
    @State private var zoom: CGFloat = 1

    private var lanes: [String: Int] {
        var result: [String: Int] = [:]
        var nextLane = 0
        for node in nodes {
            guard let id = node["node_id"] as? String else { continue }
            let parents = node["parents"] as? [String] ?? []
            let parentLane = parents.first.flatMap { result[$0] }
            let kind = node["kind"] as? String ?? "step"
            if kind == "branch" {
                nextLane = max(nextLane, (parentLane ?? 0) + 1)
                result[id] = nextLane
            } else {
                result[id] = parentLane ?? 0
            }
        }
        return result
    }

    private var points: [String: CGPoint] {
        var result: [String: CGPoint] = [:]
        let ordered = nodes.sorted {
            let leftStep = $0["step_index"] as? Int ?? 0
            let rightStep = $1["step_index"] as? Int ?? 0
            if leftStep != rightStep { return leftStep < rightStep }
            return ($0["created_at"] as? Double ?? 0) < ($1["created_at"] as? Double ?? 0)
        }
        for (index, node) in ordered.enumerated() {
            guard let id = node["node_id"] as? String else { continue }
            result[id] = CGPoint(x: 52 + CGFloat(index) * 112,
                                 y: 38 + CGFloat(lanes[id] ?? 0) * 66)
        }
        return result
    }

    private var canvasSize: CGSize {
        let p = Array(points.values)
        return CGSize(width: max(640, (p.map(\.x).max() ?? 0) + 70),
                      height: max(132, (p.map(\.y).max() ?? 0) + 48))
    }

    var body: some View {
        VStack(spacing: 8) {
            if expanded {
                HStack(spacing: 8) {
                    Button { zoom = max(0.08, zoom - 0.15) } label: {
                        Image(systemName: "minus.magnifyingglass")
                    }.buttonStyle(.bordered).help("Zoom out")
                    Text("\(Int(zoom * 100))%")
                        .font(.system(size: 10, weight: .medium, design: .monospaced))
                        .foregroundStyle(Palette.muted).frame(minWidth: 42)
                    Button { zoom = min(2.5, zoom + 0.15) } label: {
                        Image(systemName: "plus.magnifyingglass")
                    }.buttonStyle(.bordered).help("Zoom in")
                    Button("Fit graph") {
                        let widthFit = 900 / max(canvasSize.width, 1)
                        let heightFit = 500 / max(canvasSize.height, 1)
                        zoom = max(0.08, min(1.25, min(widthFit, heightFit)))
                    }.buttonStyle(.bordered)
                    Spacer()
                    Text("Scroll to pan · Pinch to zoom")
                        .font(.system(size: 9)).foregroundStyle(Palette.muted)
                }
            }
            ScrollView([.horizontal, .vertical]) {
                ZStack(alignment: .topLeading) {
                    Canvas { context, _ in
                        for node in nodes {
                            guard let id = node["node_id"] as? String,
                                  let end = points[id] else { continue }
                            let parents = node["parents"] as? [String] ?? []
                            for parent in parents {
                                guard let start = points[parent] else { continue }
                                var path = Path()
                                path.move(to: start)
                                path.addCurve(to: end,
                                              control1: CGPoint(x: start.x + 35, y: start.y),
                                              control2: CGPoint(x: end.x - 35, y: end.y))
                                let color = (node["kind"] as? String) == "branch" ? Palette.violet : Palette.line
                                context.stroke(path, with: .color(color), lineWidth: 1.4)
                            }
                        }
                    }.frame(width: canvasSize.width, height: canvasSize.height)

                    ForEach(Array(nodes.enumerated()), id: \.offset) { index, node in
                        let id = node["node_id"] as? String ?? "node-\(index)"
                        let kind = node["kind"] as? String ?? "step"
                        let point = points[id] ?? CGPoint(x: 50, y: 38)
                        Button { onSelect(id) } label: {
                            VStack(spacing: 5) {
                                ZStack {
                                    Circle().fill(id == head ? Palette.mint : Palette.panelRaised).frame(width: 22, height: 22)
                                    Circle().stroke(kind == "branch" ? Palette.violet : Palette.line, lineWidth: 1.3).frame(width: 30, height: 30)
                                    Circle().fill(id == head ? Palette.background : Palette.violet).frame(width: 5, height: 5)
                                }
                                Text("\(node["step_index"] as? Int ?? index) · \(kind)")
                                    .font(.system(size: 8, weight: .semibold, design: .monospaced))
                                    .foregroundStyle(kind == "branch" ? Palette.violet : Palette.muted)
                                    .lineLimit(1)
                            }.frame(width: 86, height: 54)
                        }.buttonStyle(.plain).contentShape(Rectangle()).position(point)
                    }
                }
                .scaleEffect(zoom, anchor: .topLeading)
                .frame(width: canvasSize.width * zoom, height: canvasSize.height * zoom, alignment: .topLeading)
            }
            .frame(height: expanded ? 510 : min(max(canvasSize.height, 132), 230))
            .simultaneousGesture(MagnificationGesture().onChanged { value in
                guard expanded else { return }
                zoom = min(2.5, max(0.08, value))
            })
        }
    }
}

private struct ControlButton: View {
    let title: String
    let icon: String
    let tint: Color
    let action: () async -> Void
    var body: some View {
        Button { Task { await action() } } label: {
            Label(title, systemImage: icon).font(.system(size: 9, weight: .semibold))
                .padding(.horizontal, 9).padding(.vertical, 8).background(tint.opacity(0.12))
                .foregroundStyle(tint).clipShape(RoundedRectangle(cornerRadius: 7))
        }.buttonStyle(.plain)
    }
}

private extension View {
    func panel() -> some View {
        padding(16).background(Palette.panel).clipShape(RoundedRectangle(cornerRadius: 13))
            .overlay(RoundedRectangle(cornerRadius: 13).stroke(Palette.line, lineWidth: 1))
    }
}
