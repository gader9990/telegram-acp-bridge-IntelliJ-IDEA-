package local.telegram;

import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import com.intellij.openapi.Disposable;
import com.intellij.openapi.application.ApplicationManager;
import com.intellij.openapi.project.Project;
import com.intellij.openapi.startup.StartupActivity;
import com.intellij.openapi.util.Disposer;
import com.intellij.ml.llm.core.chat.session.ChatSession;
import com.intellij.ml.llm.core.chat.session.ChatSourceAction;
import com.intellij.ml.llm.core.chat.session.impl.ChatSessionImpl;
import com.intellij.ml.llm.core.chat.ui.NewChatCreatorKt;
import com.intellij.ml.llm.core.chat.ui.chat.input.chatModeSelector.AIAssistantNewChatModeKt;
import com.intellij.ml.llm.core.chat.ui.chat.input.chatModeSelector.NewChatMode;
import com.intellij.ml.llm.agents.acp.session.AcpAgentSession;
import com.intellij.ml.llm.chat.session.SessionHistoryStorage;
import kotlin.coroutines.EmptyCoroutineContext;
import kotlinx.coroutines.BuildersKt;

import java.nio.charset.StandardCharsets;
import java.nio.channels.FileChannel;
import java.nio.channels.FileLock;
import java.nio.file.*;
import java.util.List;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicBoolean;

/** Only creates the configured Telegram agent's chat in this open project. */
public final class TelegramChatHelper implements StartupActivity.DumbAware {
    @Override public void runActivity(Project project) {
        if (project.getBasePath() == null) return;
        Path folder = Path.of(project.getBasePath(), "tools", "telegram-acp-bridge");
        if (!Files.isRegularFile(folder.resolve("bridge.py"))) return;
        try {
            Mailbox mailbox = new Mailbox(project, folder);
            Disposer.register(project, mailbox);
            mailbox.start();
        } catch (Exception ignored) {
            // Another IDE instance may already own this project's mailbox.
        }
    }

    private static final class Mailbox implements Disposable {
        final Project project;
        final Path folder;
        final FileChannel channel;
        final FileLock lock;
        final ScheduledExecutorService timer = Executors.newSingleThreadScheduledExecutor(r -> {
            Thread thread = new Thread(r, "telegram-chat-mailbox"); thread.setDaemon(true); return thread;
        });
        final ExecutorService creator = Executors.newSingleThreadExecutor(r -> {
            Thread thread = new Thread(r, "telegram-chat-create"); thread.setDaemon(true); return thread;
        });
        final AtomicBoolean busy = new AtomicBoolean();
        volatile boolean disposed;

        Mailbox(Project project, Path folder) throws Exception {
            this.project = project; this.folder = folder;
            channel = FileChannel.open(folder.resolve("idea-helper.lock"),
                StandardOpenOption.CREATE, StandardOpenOption.WRITE);
            FileLock acquired;
            try { acquired = channel.tryLock(); }
            catch (Exception error) { channel.close(); throw error; }
            if (acquired == null) { channel.close(); throw new IllegalStateException("Mailbox owned"); }
            lock = acquired;
        }

        void start() { timer.scheduleWithFixedDelay(this::poll, 0, 1, TimeUnit.SECONDS); }

        void write(String name, JsonObject value) throws Exception {
            Path target = folder.resolve(name), temp = folder.resolve(name + ".tmp");
            Files.writeString(temp, value.toString(), StandardCharsets.UTF_8);
            Files.move(temp, target, StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE);
        }

        void poll() {
            if (disposed || project.isDisposed()) return;
            try {
                JsonObject heartbeat = new JsonObject(); heartbeat.addProperty("version", 1);
                write("idea-helper.local.json", heartbeat);
                Path request = folder.resolve("idea-new-chat.local.json");
                if (busy.get() || !Files.isRegularFile(request)) return;
                if (Files.size(request) > 4096) { Files.delete(request); return; }
                JsonObject data = JsonParser.parseString(Files.readString(request)).getAsJsonObject();
                String id = data.get("id").getAsString();
                if (!id.matches("[0-9a-f]{32}") ||
                    !"new-chat".equals(data.get("command").getAsString())) {
                    Files.delete(request); return;
                }
                // Claim before doing anything: an IDE crash must not duplicate a chat on restart.
                Files.move(request, folder.resolve("idea-new-chat-claimed.local.json"),
                    StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE);
                if (data.get("expires").getAsDouble() < System.currentTimeMillis() / 1000.0) {
                    reply(id, null, "Запрос устарел. Нажми «Новый чат» повторно."); return;
                }
                busy.set(true);
                Future<?> task = creator.submit(() -> create(id));
                timer.schedule(() -> task.cancel(true), 115, TimeUnit.SECONDS);
            } catch (Exception ignored) {
                // Atomic replacement or project disposal can race with this polling tick.
            }
        }

        void create(String id) {
            try {
                if (disposed || project.isDisposed()) return;
                List<NewChatMode> modes = AIAssistantNewChatModeKt.getAvailableChatModes(project);
                List<NewChatMode> matches = modes.stream()
                    .filter(m -> m instanceof NewChatMode.Agent &&
                        "Codex via Telegram".equals(m.getDisplayName()) && !m.isDisabled()).toList();
                if (matches.size() != 1) {
                    reply(id, null, "В AI Assistant должен быть доступен один агент «Codex via Telegram». " +
                        "Проверь его настройку и авторизацию."); return;
                }
                ChatSession chat = BuildersKt.runBlocking(EmptyCoroutineContext.INSTANCE,
                    (scope, continuation) -> NewChatCreatorKt.createAndFocusNewChat(
                        project, matches.get(0), ChatSourceAction.NEW_CHAT, continuation));
                if (!(chat instanceof ChatSessionImpl impl) ||
                    !(impl.getAgentSession() instanceof AcpAgentSession agent)) {
                    reply(id, null, "Чат открыт, но AI Assistant не создала ACP-агента."); return;
                }
                BuildersKt.runBlocking(chat.getSessionScope().getCoroutineContext(),
                    (scope, continuation) -> agent.warmUpSessionIfNeeded(continuation));
                String sessionId = ApplicationManager.getApplication().getService(SessionHistoryStorage.class)
                    .getStoredAgentSessionId(agent.getFrontendSession().getPersistanceId());
                if (sessionId == null || sessionId.isBlank()) {
                    reply(id, null, "Чат открыт. Заверши вход/подтверждение в IntelliJ, затем выбери его через /sessions.");
                } else {
                    reply(id, sessionId, null);
                }
            } catch (Throwable error) {
                reply(id, null, "Не удалось подключить чат (" + error.getClass().getSimpleName() +
                    "). Проверь AI Assistant и совместимость Telegram Chat Helper с версией IDE.");
            } finally { busy.set(false); }
        }

        void reply(String id, String sessionId, String error) {
            if (disposed) return;
            JsonObject result = new JsonObject(); result.addProperty("id", id);
            if (sessionId != null) result.addProperty("sessionId", sessionId);
            if (error != null) result.addProperty("error", error);
            try { write("idea-new-chat-result.local.json", result); }
            catch (Exception ignored) { }
        }

        @Override public void dispose() {
            disposed = true; timer.shutdownNow(); creator.shutdownNow();
            try { Files.deleteIfExists(folder.resolve("idea-helper.local.json")); } catch (Exception ignored) { }
            try { lock.release(); } catch (Exception ignored) { }
            try { channel.close(); } catch (Exception ignored) { }
        }
    }
}
