// §app-wire — ПРОВОД: команду «Собрать» пишет НАСТОЯЩИЙ EngineClient.
//
// Зачем отдельный бинарь. Всё остальное в M-D проверяет две стороны по
// отдельности: `app/selfcheck_routing.swift` — правила приложения значением,
// `agent/selfcheck_early.py` — гейт агента на командах, которые чеканит сама
// сьюта на питоне. Между ними остаётся ровно та щель, в которой и живут ошибки
// протокола: приложение может писать команду не той формы (не то имя поля, не то
// значение build_token у скелета), и обе стороны останутся зелёными.
//
// Поэтому здесь команда пишется тем же кодом, что и в окне подтверждения
// (`EngineClient.writeConfirmBuild`), в scratch-дерево, а судит её потом
// НАСТОЯЩИЙ `agent.dispatcher.validate_command` — см. вторую половину проверки в
// `agent/selfcheck_app_routing.py`. Ни одна сторона не знает про другую ничего,
// кроме файла на диске, — как в бою.
//
// Аргументы: <support-root> <book_id>. На stdout — путь к написанной команде, на
// stderr — что приложение думает о собираемости этого манифеста.
//
// НЕ входит в бандл приложения: build/build-app.sh перечисляет свои исходники
// явно, и этого файла среди них нет.

import Foundation

@main
struct WireProbe {
    static func main() {
        guard CommandLine.arguments.count >= 3 else {
            FileHandle.standardError.write(Data("usage: wire <support-root> <book_id>\n".utf8))
            exit(2)
        }
        let root = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
        let bookID = CommandLine.arguments[2]
        let store = StateStore(supportRoot: root)
        let engine = EngineClient(store: store)
        do {
            let data = try Data(contentsOf: store.manifestURL(bookID: bookID))
            let manifest = try JSONDecoder().decode(BookManifest.self, from: data)
            // На stderr — что приложение ДУМАЕТ об этой книге. Без этой строки
            // проверка «приложение видит скелет как несобираемый» опиралась бы на
            // содержимое команды, то есть на следствие вместо решения.
            FileHandle.standardError.write(
                Data("isBuildReady=\(manifest.isBuildReady) phase=\(manifest.phaseValue)\n".utf8))
            let url = try engine.writeConfirmBuild(manifest: manifest)
            print(url.path)
        } catch {
            FileHandle.standardError.write(Data("wire failed: \(error)\n".utf8))
            exit(1)
        }
    }
}
