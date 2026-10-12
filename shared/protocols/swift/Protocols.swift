// Multiplayer AI Agent — shared wire protocols (Swift mirror).
//
// This file mirrors `shared/protocols/python/protocols.py` EXACTLY.
// Decode with a JSONDecoder configured as:
//     decoder.keyDecodingStrategy = .convertFromSnakeCase
// and encode with `.keyEncodingStrategy = .convertToSnakeCase`.
// Raw enum values (e.g. "agent.thought") are sent verbatim, so they are
// unaffected by the snake_case strategy.
//
// When you change the Python source of truth, update this file, the JSON
// Schema (envelope.schema.json), and the human spec (protocol.md) to match.

import Foundation

// MARK: - Enums

public enum MessageKind: String, Codable {
    case rpc = "rpc"
    case rpcResponse = "rpc.response"

    case agentStarted = "agent.started"
    case agentThought = "agent.thought"
    case agentToolCall = "agent.tool_call"
    case agentToolResult = "agent.tool_result"
    case agentStep = "agent.step"
    case agentDone = "agent.done"
    case agentError = "agent.error"

    case stateNodeAdded = "state.node_added"
    case stateSnapshot = "state.snapshot"
    case stateBranch = "state.branch"
    case stateMerge = "state.merge"

    case roomPresence = "room.presence"

    case steeringStatus = "steering.status"
    case steeringLockChanged = "steering.lock_changed"
    case approvalRequested = "approval.requested"
    case approvalUpdated = "approval.updated"
}

public enum RunState: String, Codable {
    case idle = "idle"
    case running = "running"
    case paused = "paused"
    case interrupted = "interrupted"
    case finished = "finished"
    case failed = "failed"
}

public enum SteeringAction: String, Codable {
    case pause = "pause"
    case resume = "resume"
    case interrupt = "interrupt"
    case inject = "inject"
}

public enum UserRole: String, Codable {
    case viewer = "viewer"
    case operatorRole = "operator"
    case steerer = "steerer"
}

// MARK: - Value objects

public struct User: Codable, Equatable {
    public var id: String
    public var name: String
    public var role: UserRole
    public init(id: String, name: String, role: UserRole = .operatorRole) {
        self.id = id; self.name = name; self.role = role
    }
}

public struct Presence: Codable, Equatable {
    public var user: User
    public var connectedAt: TimeInterval
    public var isHost: Bool
}

public struct ToolCall: Codable, Equatable {
    public var name: String
    public var args: [String: JSONValue]
}

public struct ToolResult: Codable, Equatable {
    public var name: String
    public var ok: Bool
    public var result: JSONValue?
    public var error: String?
    public var durationMs: Int
}

public struct MemoryEntry: Codable, Equatable {
    public var kind: String
    public var text: String
}

public struct Snapshot: Codable, Equatable {
    public var snapshotId: String
    public var nodeId: String
    public var stepIndex: Int
    public var runState: RunState
    public var memory: [MemoryEntry]
    public var scratch: [String: JSONValue]
    public var contentHash: String
    public var createdAt: TimeInterval
}

public struct GraphNode: Codable, Equatable {
    public var nodeId: String
    public var roomId: String
    public var label: String
    public var parents: [String]
    public var stepIndex: Int
    public var kind: String
    public var snapshotId: String
    public var createdAt: TimeInterval
    public var meta: [String: JSONValue]
}

public struct GraphEdge: Codable, Equatable {
    public var src: String
    public var dst: String
    public var weight: Double
}

public struct SteeringLock: Codable, Equatable {
    public var roomId: String
    public var holderId: String?
    public var holderName: String?
    public var token: String
    public var acquiredAt: TimeInterval
}

// MARK: - Envelope

public struct Envelope: Codable, Equatable {
    public var type: MessageKind
    public var v: Int
    public var id: String
    public var roomId: String
    public var seq: Int
    public var ts: TimeInterval
    public var sender: String
    public var replyTo: String?
    public var correlationId: String?
    public var payload: [String: JSONValue]

    public init(
        type: MessageKind,
        v: Int = 1,
        id: String = UUID().uuidString.replacingOccurrences(of: "-", with: ""),
        roomId: String = "",
        seq: Int = 0,
        ts: TimeInterval = Date().timeIntervalSince1970,
        sender: String = "",
        replyTo: String? = nil,
        correlationId: String? = nil,
        payload: [String: JSONValue] = [:]
    ) {
        self.type = type
        self.v = v
        self.id = id
        self.roomId = roomId
        self.seq = seq
        self.ts = ts
        self.sender = sender
        self.replyTo = replyTo
        self.correlationId = correlationId
        self.payload = payload
    }
}

// MARK: - RPC

public struct RPCError: Codable, Equatable {
    public var code: String
    public var message: String
}

public struct RPCRequest: Codable, Equatable {
    public var id: String
    public var method: String
    public var args: [String: JSONValue]
    public init(id: String = UUID().uuidString.replacingOccurrences(of: "-", with: ""),
                method: String, args: [String: JSONValue] = [:]) {
        self.id = id; self.method = method; self.args = args
    }
}

public struct RPCResponse: Codable, Equatable {
    public var id: String
    public var replyTo: String
    public var ok: Bool
    public var result: JSONValue?
    public var error: RPCError?
}

public enum RPC {
    static let roomCreate = "room.create"
    static let roomJoin = "room.join"
    static let roomLeave = "room.leave"
    static let roomInfo = "room.info"

    static let agentStart = "agent.start"
    static let agentStatus = "agent.status"

    static let steeringAcquire = "steering.acquire"
    static let steeringRelease = "steering.release"
    static let steeringHandoff = "steering.handoff"
    static let steeringPause = "steering.pause"
    static let steeringResume = "steering.resume"
    static let steeringInterrupt = "steering.interrupt"
    static let steeringInject = "steering.inject"
    static let steeringLock = "steering.lock"
    static let steeringSetEnv = "steering.set_env"

    static let approvalVote = "approval.vote"

    static let stateGraph = "state.graph"
    static let stateSnapshot = "state.snapshot"
    static let stateScrub = "state.scrub"
    static let stateBranch = "state.branch"
    static let stateMerge = "state.merge"
}

// MARK: - Arbitrary JSON

/// A small recursive JSON type for arbitrary payloads/results.
public enum JSONValue: Codable, Equatable {
    case null
    case bool(Bool)
    case int(Int)
    case double(Double)
    case string(String)
    case array([JSONValue])
    case object([String: JSONValue])

    public init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null; return }
        if let b = try? c.decode(Bool.self) { self = .bool(b); return }
        if let i = try? c.decode(Int.self) { self = .int(i); return }
        if let d = try? c.decode(Double.self) { self = .double(d); return }
        if let s = try? c.decode(String.self) { self = .string(s); return }
        if let a = try? c.decode([JSONValue].self) { self = .array(a); return }
        if let o = try? c.decode([String: JSONValue].self) { self = .object(o); return }
        self = .null
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .null: try c.encodeNil()
        case .bool(let b): try c.encode(b)
        case .int(let i): try c.encode(i)
        case .double(let d): try c.encode(d)
        case .string(let s): try c.encode(s)
        case .array(let a): try c.encode(a)
        case .object(let o): try c.encode(o)
        }
    }
}
