import { defineStore } from "pinia";
import { ref } from "vue";

import type { UserState } from "@/stores/interface";

export const useUserStore = defineStore(
  "geeker-user",
  () => {
    const token = ref<string>("");
    const setToken = (newToken: string) => {
      token.value = newToken;
    };

    const userInfo = ref<UserState["userInfo"]>({ name: "财务同学" });
    const setUserInfo = (newUserInfo: UserState["userInfo"]) => {
      userInfo.value = newUserInfo;
    };

    return {
      token,
      userInfo,
      setToken,
      setUserInfo
    };
  },
  {
    persist: {
      key: "geeker-user",
      storage: localStorage
    }
  }
);
