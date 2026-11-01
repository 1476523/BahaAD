// 共用的「多選手勢」：一般點擊＝單選（清掉其他）、再點一次唯一選中的＝取消、
// Ctrl/Cmd+點擊＝個別加選、Shift+點擊＝從上次點擊位置區間選、左鍵按住拖曳＝連續加選。
// 觸控裝置：輕點＝單選，長按（約 0.35 秒）後不放開、拖曳手指＝從長按處到手指所在
// 區間選取（使用者 2026-09-10）。
// 番劇詳細頁的集數選取器（episode_picker.js）跟資料庫整頓頁（db_cleanup.html）共用。
//
// 每個 container 各自算一組（anchor／區間都不跨 container）。每個 item 內要有一個
// <input type="checkbox">，狀態由手勢控制、擋掉瀏覽器原生 click 切換。
(function () {
  "use strict";

  window.initMultiSelect = function (container, opts) {
    opts = opts || {};
    var itemSelector = opts.itemSelector || ".select-item";
    var selectedClass = opts.selectedClass || "selected";
    // 點唯一選中的那一格再點一次＝整個取消（集數選取器要，一般清單也沿用）
    var toggleLoneClick = opts.toggleLoneClick !== false;
    // 每次選取狀態有變（點擊／拖曳結束）就呼叫——集數選取器用來決定要不要顯示「播放」鈕
    var onChange = typeof opts.onChange === "function" ? opts.onChange : null;
    function notifyChange() {
      if (onChange) onChange(container);
    }

    var items = Array.prototype.slice.call(container.querySelectorAll(itemSelector));
    if (!items.length) {
      return;
    }
    var anchorIndex = null;
    var dragging = false;

    function checkboxOf(item) {
      return item.querySelector("input[type=checkbox]");
    }

    function setChecked(item, value) {
      var cb = checkboxOf(item);
      if (cb) {
        cb.checked = value;
      }
      item.classList.toggle(selectedClass, value);
    }

    function checkedCount() {
      return items.filter(function (it) {
        var cb = checkboxOf(it);
        return cb && cb.checked;
      }).length;
    }

    items.forEach(function (item, index) {
      item.addEventListener("mousedown", function (e) {
        if (e.button !== 0) {
          return;
        }
        e.preventDefault(); // 不用原生 checkbox 點擊，全部自己控制，才能一致支援 shift／拖曳
        dragging = true;

        if (e.shiftKey && anchorIndex !== null) {
          var start = Math.min(anchorIndex, index);
          var end = Math.max(anchorIndex, index);
          items.forEach(function (it, i) {
            setChecked(it, i >= start && i <= end);
          });
        } else if (e.ctrlKey || e.metaKey) {
          var cb = checkboxOf(item);
          setChecked(item, !(cb && cb.checked));
          anchorIndex = index;
        } else {
          var current = checkboxOf(item);
          var deselect = toggleLoneClick && current && current.checked && checkedCount() === 1;
          items.forEach(function (it, i) {
            setChecked(it, i === index && !deselect);
          });
          anchorIndex = deselect ? null : index;
        }
        notifyChange();
      });

      item.addEventListener("mouseenter", function () {
        if (dragging) {
          setChecked(item, true);
          notifyChange();
        }
      });

      // <label> 包 checkbox 時，mouseup 後瀏覽器會補一次 click 去切換 checkbox——會把
      // 上面 mousedown 設好的狀態立刻反轉。這裡把 click 全部擋掉、不做事。
      item.addEventListener("click", function (e) {
        e.preventDefault();
      });
    });

    document.addEventListener("mouseup", function () {
      dragging = false;
    });

    // ---- 觸控：長按進入「拖曳區間選取」模式 --------------------------------
    // 觸控裝置沒有 hover，上面的 mouseenter 連續加選失效；iOS 長按名稱又常被系統
    // 接走變成文字選取／預覽。做法：touchstart 起一個 0.35 秒計時器，期間手指移動
    // 超過門檻就當成捲動、取消；計時器到點沒被取消 → armed，先把長按那格設成唯一
    // 選取（當 anchor），之後 touchmove 擋掉捲動、依手指位置做 anchor→現在格的區間選。
    var LONGPRESS_MS = 350;
    var MOVE_CANCEL_PX = 12;
    var pressTimer = null;
    var pressStart = null; // {x, y, index}
    var touchArmed = false;

    function clearPressTimer() {
      if (pressTimer) {
        clearTimeout(pressTimer);
        pressTimer = null;
      }
    }

    function itemIndexFromPoint(x, y) {
      var el = document.elementFromPoint(x, y);
      if (!el) return -1;
      var item = el.closest ? el.closest(itemSelector) : null;
      if (!item || !container.contains(item)) return -1;
      return items.indexOf(item);
    }

    function selectRange(a, b) {
      var start = Math.min(a, b);
      var end = Math.max(a, b);
      items.forEach(function (it, i) {
        setChecked(it, i >= start && i <= end);
      });
      notifyChange();
    }

    items.forEach(function (item, index) {
      item.addEventListener(
        "touchstart",
        function (e) {
          if (e.touches.length !== 1) {
            clearPressTimer();
            touchArmed = false;
            return;
          }
          var t = e.touches[0];
          pressStart = { x: t.clientX, y: t.clientY, index: index };
          touchArmed = false;
          clearPressTimer();
          pressTimer = setTimeout(function () {
            pressTimer = null;
            touchArmed = true;
            anchorIndex = index;
            items.forEach(function (it, i) {
              setChecked(it, i === index);
            });
            notifyChange();
          }, LONGPRESS_MS);
        },
        { passive: true }
      );

      item.addEventListener(
        "touchmove",
        function (e) {
          if (!pressStart) return;
          var t = e.touches[0];
          if (!touchArmed) {
            // 還在判斷是長按還是捲動——移動太多就是捲動，放掉
            var dx = Math.abs(t.clientX - pressStart.x);
            var dy = Math.abs(t.clientY - pressStart.y);
            if (dx > MOVE_CANCEL_PX || dy > MOVE_CANCEL_PX) {
              clearPressTimer();
              pressStart = null;
            }
            return;
          }
          // 已進入拖曳選取：擋掉捲動，依手指所在格做區間選
          e.preventDefault();
          var idx = itemIndexFromPoint(t.clientX, t.clientY);
          if (idx !== -1 && anchorIndex !== null) {
            selectRange(anchorIndex, idx);
          }
        },
        { passive: false }
      );

      function endTouch(e) {
        clearPressTimer();
        pressStart = null;
        if (touchArmed) {
          // 擋掉隨後補發的 mousedown／click，否則單選邏輯會把多選收回成一格
          if (e.cancelable) e.preventDefault();
          touchArmed = false;
          notifyChange();
        }
      }
      item.addEventListener("touchend", endTouch);
      item.addEventListener("touchcancel", endTouch);
    });
  };
})();
