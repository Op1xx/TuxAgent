# Матрица поддержки дистрибутивов и пакетных менеджеров

Документ фиксирует, что именно реализовано и проверено на этапе 2. Он не
заменяет `/etc/os-release` и `man` на конкретной системе.

## Как выбирается бэкенд

1. `distro.detect_distro()` читает `/etc/os-release` и разбирает `ID`,
   `ID_LIKE`, `VERSION_ID`, `PRETTY_NAME`.
2. `distro.detect_family()` сначала ищет точный `ID`, затем перебирает
   `ID_LIKE` слева направо.
3. `package_managers.create_package_manager()` выбирает бэкенд по семейству и
   проверяет наличие утилит через `shutil.which()`.
4. Ручной профиль в настройках (`manual`) заменяет только выбор семейства;
   `create_package_manager_for_settings()` применяет режим `auto`/`manual` и
   возвращает бэкенд вместе с предупреждением. Если профиль не совпадает с
   реальной системой, GUI показывает предупреждение, а запуск пишет его в лог.

## Бэкенды

| Семейство | Дистрибутивы | Бэкенд | Проверка установки | Поиск |
|---|---|---|---|---|
| `debian` | Debian, Ubuntu, Astra Linux | `apt-get` + `apt-cache` + `dpkg-query` | `dpkg-query -W -f=${Status}` | `apt-cache search --names-only` |
| `fedora` | Fedora, ROSA | `dnf` + `rpm` | `rpm -q` | `dnf --quiet search` |
| `arch` | Arch Linux и производные | `pacman` | `pacman -Q` | `pacman -Ss` |
| `suse` | openSUSE Leap/Tumbleweed | `zypper` + `rpm` | `rpm -q` | `zypper --non-interactive search` |
| `alt` | ALT Linux | `apt-get` + `apt-cache` + `rpm` | `rpm -q` | `apt-cache search --names-only` |
| `alt` (опционально) | ALT Linux | `epm` + `rpm` | `rpm -q` | `epm search` |

Для ALT по умолчанию используется `apt`. `epm` выбирается только при
`prefer_epm=True` или как фолбэк, если `apt-get`/`apt-cache` недоступны.
Отдельного переключателя в GUI для `epm` пока нет. Команды `epm` сверены с
`man eepm` (`epm update` — обновление баз репозиториев, `epm clean` — очистка
кэша, `--dry-run` поддержан для remove), но на живой ALT не запускались.

## Команды

Все команды собираются как argv. `shell=True`, конкатенация строк и
интерполяция пользовательского ввода не используются.

| Операция | apt | dnf | pacman | zypper | epm |
|---|---|---|---|---|---|
| install | `apt-get install --yes` | `dnf install --assumeyes` | `pacman -S --noconfirm` | `zypper --non-interactive install` | `epm install -y` |
| remove | `apt-get remove --yes` | `dnf remove --assumeyes` | `pacman -R --noconfirm` | `zypper --non-interactive remove` | `epm remove -y` |
| update index | `apt-get update` | `dnf makecache` | `pacman -Sy` | `zypper --non-interactive refresh` | `epm update` |
| clean cache | `apt-get clean` | `dnf clean all` | `pacman -Sc --noconfirm` | `zypper --non-interactive clean --all` | `epm clean` |

Изменяющие операции требуют root. Если приложение запущено не от root,
`CommandRunner` добавляет `pkexec`; для apt дополнительно передаётся
`DEBIAN_FRONTEND=noninteractive` через `env`. `sudo` не используется.
Dry-run-команды выполняются без `pkexec`, потому что не должны менять систему.

## Dry-run

| Бэкенд | install | remove | update index | clean cache |
|---|---|---|---|---|
| apt / ALT apt | `apt-get --simulate ...` | `apt-get --simulate ...` | `apt-get --simulate update` | `apt-get --simulate clean` |
| dnf | `--setopt=tsflags=test` | `--setopt=tsflags=test` | отказ | отказ |
| pacman | `pacman -S --print` | `pacman -R --print` | отказ | отказ |
| zypper | `--dry-run` | `--dry-run` | `--dry-run refresh` | `--dry-run clean --all` |
| epm | отказ | `--dry-run remove` | отказ | отказ |

«Отказ» — это `DryRunNotSupportedError` с объяснением: флаг не подтверждён,
поэтому операция не выполняется. Это сделано намеренно: лучше не показать
план, чем принять изменяющую команду за безопасную.

## Что проверено

### Arch Linux — live-проверка

Проверено на текущей машине (Arch Linux, pacman 7.x):

- определение дистрибутива: `ID=arch`, семейство `arch`;
- `pacman -Q bash` → установлен, несуществующий пакет → код 1;
- `pacman -Ss vlc` → 86 результатов, парсер корректно извлёк имена и описания;
- `pacman -S --print vlc` → код 0, вывод содержит только URL целей, установка
  не выполнялась;
- `pacman -R --print bash` → код 1 из-за зависимостей, удаление не выполнялось.

### Остальные семейства

Проверены только unit-тестами: сборка argv, валидация имён, парсинг типового
вывода поиска, выбор бэкенда и обработка отсутствия утилит. Реальные
`apt`, `dnf`, `zypper` и `epm` в тестах не запускаются.

## Фикстуры `/etc/os-release`

Фикстуры лежат в `tests/fixtures/os-release/`:

- `debian`, `ubuntu`, `fedora`, `arch`, `opensuse-leap` — по официальным
  форматам этих дистрибутивов;
- `astra`, `rosa`, `alt` — **синтетические**: точный набор полей на живой
  системе не проверялся, в файлах стоит комментарий;
- `unknown` — проверка того, что неизвестный `ID` не угадывается.

При появлении доступа к ВМ Astra/ALT/ROSA фикстуры нужно заменить реальными
файлами и повторить проверку.

## Статус

| Пункт | Статус |
|---|---|
| Детект `/etc/os-release` | реализовано, покрыто тестами |
| Семейства debian/fedora/arch/suse/alt/unknown | реализовано |
| Интерфейс `PackageManager` | реализовано |
| Бэкенды apt/dnf/pacman/zypper/ALT apt | реализовано, argv покрыт тестами |
| EPM | реализован как опциональный, live-проверки нет |
| Валидация имён пакетов | реализовано, покрыто тестами |
| Запрет shell | реализовано, проверяется тестом `shell=False` |
| `pkexec` для root | реализовано, покрыто тестом сборки argv |
| Dry-run | реализован там, где флаг подтверждён |
| Ручной override + предупреждение | реализовано в настройках и при сохранении |
| Live-проверка Arch | выполнена |
| Live-проверка Astra/ALT/ROSA | не выполнена, нужны ВМ |
