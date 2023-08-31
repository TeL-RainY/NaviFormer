import random, copy, struct, pymongo, torch, math
import numpy as np
from torch.utils.data import Dataset

class NaviDataset(Dataset):
    def __init__(self, host, database='DataBase', collection='Collection'):
        super(NaviDataset, self).__init__()
        self.client = pymongo.MongoClient(host)
        self.database = self.client[database]
        self.collection = self.database[collection]
        self.Len = self.collection.estimated_document_count()

    @classmethod
    def __decode_data(cls, bytes_data):
        type_dict = {
            1 : (1, np.uint8),
            2 : (2, np.float16),
            3 : (4, np.float32),
            4 : (8, np.float64),
        }
        index, length = 0, len(bytes_data)
        list_np_data = []
        while index < length:
            c_type = struct.unpack(">h", bytes_data[index: index+2])[0]
            tb, t = type_dict[c_type]
            count, shape_dim = struct.unpack(">II", bytes_data[index+2: index+10])
            shape = struct.unpack(">" + "I" * shape_dim,
                                  bytes_data[index + 10: index + 10 + shape_dim * 4])
            index = index + 10 + shape_dim * 4
            each_bytes_data = bytes_data[index: index + count*tb]
            list_np_data.append(np.frombuffer(each_bytes_data, dtype=t).reshape(*shape))
            index += count*tb
        return list_np_data
    
    @classmethod
    def __encode_data(cls, np_list_data):
        def type_encode(data_dtype: np.dtype):
            if data_dtype == np.uint8:
                return 1
            elif data_dtype == np.float16:
                return 2
            elif data_dtype == np.float32:
                return 3
            elif data_dtype == np.float64:
                return 4
            else:
                raise ValueError
        bytes_out = b""
        for np_data in np_list_data:
            shape = np_data.shape
            shape_dim = len(shape)
            count = 1
            for shape_i in shape:
                count *= shape_i
            bytes_out += struct.pack(">h", type_encode(np_data.dtype))
            bytes_out += struct.pack(">II", count, shape_dim)
            bytes_out += struct.pack(">" + "I" * shape_dim, *shape)
            bytes_out += np_data.tobytes()
        return bytes_out

    @classmethod
    def __discrete_action(cls, action):
        def index_count(input, _min, _max, _resolution):
            input = min(_max, max(_min, input)) - _min
            return math.floor(input/_resolution)
        
        output = np.zeros(shape=(1, 11*19) ,dtype=float)
        v_num = index_count(action[0][0], 0.0-0.01, 1.0, 0.1)
        w_num = index_count(action[0][1], -0.9-0.01, 0.9, 0.1)
        index = v_num*19+w_num
        output[0][index] = 1.0
        return output

    @classmethod
    def __data_to_exp(cls, data):
        exp = {'laser':[], 'vector':[], 'action':[]}
        for key in exp.keys():
            exp[key] = NaviDataset.__decode_data(data[key])
        exp['discrete_action'] = []
        for i in range(len(exp['laser'])):
            exp['discrete_action'].append(NaviDataset.__discrete_action(exp['action'][i]))
        index = random.randint(0, len(exp['laser'])-1)
        for key in exp.keys():
            exp[key] = exp[key][index]

        return exp

    def __len__(self):
        return self.Len
    
    def __getitem__(self, index):
        return NaviDataset.__data_to_exp(self.collection.find_one({'_id': int(index)}))
        # return NaviDataset.__data_to_exp(self.collection.aggregate([{'$sample': {'size': 1}}]).next())

class NaviCollateFN:
    def __init__(self, max_len):
        self.max_len = max_len

    def collate_fn(self, batch):
        exp_batch = {'laser':[], 'vector':[], 'discrete_action':[]}

        for exp in batch:
            for key in exp_batch.keys():
                exp_batch[key].append(exp[key])

        for key in exp_batch.keys():
            exp_batch[key] = torch.from_numpy(np.concatenate(exp_batch[key], axis=0)).to(dtype=torch.float32)

        return exp_batch['laser'], exp_batch['vector'], exp_batch['discrete_action']