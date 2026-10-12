import Foundation
import Combine
import AppKit

struct RoomSummary: Identifiable, Decodable {
    let id: String
    var name: String
    var run_state: String
    var member_count: Int?
    var share_url: String?
}

struct AgentEvent: Identifiable {
    let id = UUID()
    let type: String
    let payload: [String: Any]
    let receivedAt = Date()
}

@MainActor
final class RoomModel: ObservableObject {
    @Published var rooms: [RoomSummary] = []
    @Published var selectedRoom: RoomSummary?
    @Published var events: [AgentEvent] = []
    @Published var graph: [String: Any] = [:]
    @Published var members: [[String: Any]] = []
    @Published var runState = "idle"
    @Published var steeringToken: String?
    @Published var steeringHolder: String?
    @Published var connectionState = "Disconnected"
    @Published var errorMessage: String?
    @Published var approvalGates: [[String: Any]] = []
    @Published var finalResult: String?
    @Published var isAuthenticated = false

    var baseURL = URL(string: ProcessInfo.processInfo.environment["AGENT_SERVER"] ?? "http://127.0.0.1:8000")!
    private var socket: URLSessionWebSocketTask?
    private var reconnectTask: Task<Void, Never>?
    private var heartbeatTask: Task<Void, Never>?
    private var pending: [String: CheckedContinuation<[String: Any], Error>] = [:]
    var userID = ""
    var userName = Host.current().localizedName ?? "Teammate"

    func refreshRooms() async {
        do {
            let (authData, authResponse) = try await URLSession.shared.data(from: baseURL.appending(path: "auth/me"))
            guard (authResponse as? HTTPURLResponse)?.statusCode == 200 else {
                isAuthenticated = false
                return
            }
            let auth = try JSONSerialization.jsonObject(with: authData) as? [String: Any] ?? [:]
            let user = auth["user"] as? [String: Any] ?? [:]
            userID = user["id"] as? String ?? ""
            userName = user["name"] as? String ?? "Teammate"
            isAuthenticated = !userID.isEmpty
            guard isAuthenticated else { return }
            let (data, _) = try await URLSession.shared.data(from: baseURL.appending(path: "rooms"))
            rooms = try JSONDecoder().decode([RoomSummary].self, from: data)
        } catch { errorMessage = error.localizedDescription }
    }

    func authenticate(email: String, password: String, name: String? = nil) async {
        do {
            var request = URLRequest(url: baseURL.appending(path: "auth/\(name == nil ? "login" : "register")"))
            request.httpMethod = "POST"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            var body: [String: String] = ["email": email, "password": password]
            if let name { body["name"] = name }
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
            let (data, response) = try await URLSession.shared.data(for: request)
            guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
                let payload = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
                throw NSError(domain: "MultiplayerAuth", code: (response as? HTTPURLResponse)?.statusCode ?? 0,
                              userInfo: [NSLocalizedDescriptionKey: payload?["detail"] as? String ?? "Sign in failed."])
            }
            await refreshRooms()
        } catch { errorMessage = error.localizedDescription }
    }

    func signOut() async {
        var request = URLRequest(url: baseURL.appending(path: "auth/logout"))
        request.httpMethod = "POST"
        _ = try? await URLSession.shared.data(for: request)
        disconnect()
        isAuthenticated = false
        userID = ""
        userName = "Teammate"
        rooms = []
        selectedRoom = nil
    }

    func createRoom() async {
        do {
            var request = URLRequest(url: baseURL.appending(path: "rooms"))
            request.httpMethod = "POST"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONSerialization.data(withJSONObject: ["name": "Team workspace"])
            let (data, _) = try await URLSession.shared.data(for: request)
            let room = try JSONDecoder().decode(RoomSummary.self, from: data)
            rooms.insert(room, at: 0)
            await connect(to: room)
        } catch { errorMessage = error.localizedDescription }
    }

    func connect(to room: RoomSummary) async {
        reconnectTask?.cancel()
        reconnectTask = nil
        heartbeatTask?.cancel()
        heartbeatTask = nil
        socket?.cancel(with: .goingAway, reason: nil)
        selectedRoom = room
        events = []
        graph = [:]
        finalResult = nil
        steeringToken = nil
        openSocket(for: room)
    }

    private func openSocket(for room: RoomSummary) {
        var components = URLComponents(url: baseURL, resolvingAgainstBaseURL: false)!
        components.scheme = baseURL.scheme == "https" ? "wss" : "ws"
        components.path = "/ws/\(room.id)"
        let task = URLSession.shared.webSocketTask(with: components.url!)
        socket = task
        task.resume()
        connectionState = "Connecting"
        receiveNext(from: task)
        heartbeatTask?.cancel()
        heartbeatTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self, self.socket === task else { return }
                _ = try? await self.call("ping")
                try? await Task.sleep(nanoseconds: 20_000_000_000)
            }
        }
    }

    func disconnect() {
        reconnectTask?.cancel()
        reconnectTask = nil
        heartbeatTask?.cancel()
        heartbeatTask = nil
        socket?.cancel(with: .goingAway, reason: nil)
        socket = nil
        failPendingCalls(with: URLError(.cancelled))
        connectionState = "Disconnected"
    }

    private func failPendingCalls(with error: Error) {
        let continuations = Array(pending.values)
        pending.removeAll()
        continuations.forEach { $0.resume(throwing: error) }
    }

    func call(_ method: String, args: [String: Any] = [:]) async throws -> [String: Any] {
        guard let socket else { throw URLError(.notConnectedToInternet) }
        let id = UUID().uuidString
        let frame: [String: Any] = ["type": "rpc", "v": 1, "id": UUID().uuidString,
                                    "room_id": selectedRoom?.id ?? "", "seq": 0,
                                    "ts": Date().timeIntervalSince1970, "sender": userID,
                                    "payload": ["id": id, "method": method, "args": args]]
        let data = try JSONSerialization.data(withJSONObject: frame)
        return try await withCheckedThrowingContinuation { continuation in
            pending[id] = continuation
            socket.send(.string(String(decoding: data, as: UTF8.self))) { [weak self] error in
                if let error {
                    Task { @MainActor in self?.pending.removeValue(forKey: id)?.resume(throwing: error) }
                }
            }
        }
    }

    func acquireSteering() async {
        do {
            let result = try await call("steering.acquire")
            let lock = result["lock"] as? [String: Any] ?? [:]
            steeringToken = lock["token"] as? String
            steeringHolder = userName
        } catch { errorMessage = error.localizedDescription }
    }

    func handoffSteering(to memberID: String) async {
        guard let steeringToken else { return }
        do {
            _ = try await call("steering.handoff", args: ["token": steeringToken, "target_id": memberID])
            self.steeringToken = nil
        } catch { errorMessage = error.localizedDescription }
    }

    func releaseSteering() async {
        guard let steeringToken else { return }
        do {
            _ = try await call("steering.release", args: ["token": steeringToken])
            self.steeringToken = nil
        } catch { errorMessage = error.localizedDescription }
    }

    func steer(_ method: String, text: String? = nil) async {
        if steeringToken == nil { await acquireSteering() }
        guard let steeringToken else { return }
        var args: [String: Any] = ["token": steeringToken]
        if let text { args["text"] = text }
        do { _ = try await call("steering.\(method)", args: args) }
        catch { errorMessage = error.localizedDescription }
    }

    func start(task: String) async {
        do { _ = try await call("agent.start", args: ["task": task]) }
        catch { errorMessage = error.localizedDescription }
    }

    func vote(gateID: String, approve: Bool) async {
        do {
            let gate = try await call("approval.vote", args: ["gate_id": gateID, "approve": approve])
            approvalGates.removeAll { ($0["gate_id"] as? String) == gateID }
            if (gate["status"] as? String) == "pending" { approvalGates.append(gate) }
        } catch { errorMessage = error.localizedDescription }
    }

    func copyInvite() {
        guard let room = selectedRoom else { return }
        Task {
            do {
                var request = URLRequest(url: baseURL.appending(path: "rooms/\(room.id)/invite"))
                request.httpMethod = "POST"
                let (data, response) = try await URLSession.shared.data(for: request)
                guard (response as? HTTPURLResponse)?.statusCode == 200,
                      let payload = try JSONSerialization.jsonObject(with: data) as? [String: String],
                      let invite = payload["share_url"] else {
                    throw URLError(.badServerResponse)
                }
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(invite, forType: .string)
            } catch { errorMessage = error.localizedDescription }
        }
    }

    func refreshGraph() async {
        do {
            let result = try await call("state.graph")
            graph = result
        } catch { errorMessage = error.localizedDescription }
    }

    var executionTranscript: String {
        events.reversed().filter {
            $0.type.hasPrefix("agent.") || $0.type == "approval.requested" || $0.type == "approval.updated"
        }.map { event in
            let time = event.receivedAt.formatted(date: .omitted, time: .shortened)
            return "[\(time)] \(event.type.uppercased())\n\(eventSummary(event))"
        }.joined(separator: "\n\n")
    }

    func copyExecutionResult() {
        let content = [finalResult.map { "FINAL RESULT\n\($0)" },
                       executionTranscript.isEmpty ? nil : "EXECUTION TRANSCRIPT\n\(executionTranscript)"]
            .compactMap { $0 }.joined(separator: "\n\n")
        guard !content.isEmpty else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(content, forType: .string)
    }

    private func receiveNext(from task: URLSessionWebSocketTask) {
        task.receive { [weak self] result in
            Task { @MainActor in
                guard let self else { return }
                guard self.socket === task else { return }
                switch result {
                case .failure:
                    self.connectionState = "Disconnected"
                    self.failPendingCalls(with: URLError(.notConnectedToInternet))
                    if let room = self.selectedRoom { self.scheduleReconnect(for: room) }
                case .success(let message):
                    self.handle(message)
                    self.receiveNext(from: task)
                }
            }
        }
    }

    private func scheduleReconnect(for room: RoomSummary) {
        guard reconnectTask == nil else { return }
        reconnectTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 1_500_000_000)
            guard !Task.isCancelled, let self, self.selectedRoom?.id == room.id else { return }
            self.reconnectTask = nil
            self.openSocket(for: room)
        }
    }

    private func handle(_ message: URLSessionWebSocketTask.Message) {
        let data: Data
        switch message {
        case .data(let value): data = value
        case .string(let value): data = Data(value.utf8)
        @unknown default: return
        }
        guard let frame = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let type = frame["type"] as? String,
              let payload = frame["payload"] as? [String: Any] else { return }
        connectionState = "Connected"
        if type == "rpc.response", let reply = payload["reply_to"] as? String,
           let continuation = pending.removeValue(forKey: reply) {
            if (payload["ok"] as? Bool) == true {
                continuation.resume(returning: payload["result"] as? [String: Any] ?? [:])
            } else {
                let error = payload["error"] as? [String: Any]
                continuation.resume(throwing: NSError(domain: "AgentRPC", code: 1,
                    userInfo: [NSLocalizedDescriptionKey: error?["message"] as? String ?? "Request failed"]))
            }
            return
        }
        if type == "state.snapshot" {
            if let value = payload["graph"] as? [String: Any] {
                graph = value
                if let recovered = latestAssistantResult(in: value) { finalResult = recovered }
            }
            runState = payload["run_state"] as? String ?? runState
            approvalGates = payload["approvals"] as? [[String: Any]] ?? approvalGates
        } else if type == "state.node_added", let value = payload["node"] as? [String: Any] {
            upsertNode(value, snapshot: payload["snapshot"] as? [String: Any])
        } else if type == "room.presence" {
            members = payload["members"] as? [[String: Any]] ?? []
        } else if type == "steering.lock_changed" {
            let lock = payload["lock"] as? [String: Any] ?? [:]
            steeringHolder = lock["holder_name"] as? String
            if (lock["holder_id"] as? String) != userID { steeringToken = nil }
            if let token = payload["your_token"] as? String { steeringToken = token }
        } else if type == "steering.status" || type == "agent.done" {
            runState = payload["state"] as? String ?? runState
            if type == "agent.done" {
                if let summary = payload["summary"] as? String, !summary.isEmpty { finalResult = summary }
                else if let recovered = latestAssistantResult(in: graph) { finalResult = recovered }
                else if (payload["state"] as? String) == "failed",
                        let failure = events.first(where: { $0.type == "agent.error" })?.payload["message"] as? String {
                    finalResult = failure
                }
            }
        } else if type == "approval.requested" || type == "approval.updated" {
            let gateID = payload["gate_id"] as? String ?? ""
            approvalGates.removeAll { ($0["gate_id"] as? String) == gateID }
            if (payload["status"] as? String) == "pending" { approvalGates.append(payload) }
        } else if type == "agent.started" {
            runState = "running"
            finalResult = nil
        }
        events.insert(AgentEvent(type: type, payload: payload), at: 0)
        if events.count > 300 { events.removeLast(events.count - 300) }
    }

    private func upsertNode(_ node: [String: Any], snapshot: [String: Any]?) {
        let nodeID = node["node_id"] as? String
        var updated = graph
        var nodes = updated["nodes"] as? [[String: Any]] ?? []
        if let nodeID { nodes.removeAll { ($0["node_id"] as? String) == nodeID } }
        nodes.append(node)
        nodes.sort { ($0["step_index"] as? Int ?? 0) < ($1["step_index"] as? Int ?? 0) }
        updated["nodes"] = nodes
        if let nodeID { updated["head_id"] = nodeID }
        if let snapshot, let snapshotID = snapshot["snapshot_id"] as? String {
            var snapshots = updated["snapshots"] as? [String: Any] ?? [:]
            snapshots[snapshotID] = snapshot
            updated["snapshots"] = snapshots
        }
        graph = updated
        if let recovered = latestAssistantResult(in: updated), runState != "running" { finalResult = recovered }
    }

    private func latestAssistantResult(in graph: [String: Any]) -> String? {
        let nodes = graph["nodes"] as? [[String: Any]] ?? []
        let snapshots = graph["snapshots"] as? [String: [String: Any]] ?? [:]
        let headID = graph["head_id"] as? String
        guard let latest = nodes.first(where: { ($0["node_id"] as? String) == headID })
                ?? nodes.max(by: { ($0["step_index"] as? Int ?? 0) < ($1["step_index"] as? Int ?? 0) }),
              let snapshotID = latest["snapshot_id"] as? String,
              let snapshot = snapshots[snapshotID],
              let memory = snapshot["memory"] as? [[String: Any]] else { return nil }
        return memory.last(where: { ($0["kind"] as? String) == "assistant" })?["text"] as? String
    }

    private func eventSummary(_ event: AgentEvent) -> String {
        if let text = event.payload["text"] as? String { return text }
        if let message = event.payload["message"] as? String { return message }
        if let summary = event.payload["summary"] as? String { return summary }
        if let tool = event.payload["tool"] as? [String: Any] { return "\(tool["name"] ?? "tool") · \(tool["args"] ?? [:])" }
        if let result = event.payload["result"] { return "Result · \(String(describing: result))" }
        return event.payload["state"] as? String ?? event.type
    }
}
