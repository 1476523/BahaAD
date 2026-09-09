// 共用的「多選手勢」：一般點擊＝單選（清掉其他）、再點一次唯一選中的＝取消、
// Ctrl/Cmd+點擊＝個別加選、Shift+點擊＝從上次點擊位置區間選、左鍵按住拖曳＝連續加選。
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
  };
})();
